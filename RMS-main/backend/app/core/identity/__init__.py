"""Identity admission: who this platform admits once the IdP has authenticated
them.

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §3.

Authentication answers "is this token genuine". Admission answers the separate
question "is this identity one we let in", and it has two rules:

* the email-domain allowlist (:mod:`app.core.identity.email_domain`);
* the account's ``users.status``, below.

Both are enforced at every door that turns a token into a principal --
``app/api/deps.py`` (HTTP), ``app/api/v1/ws.py`` (console WebSocket) and
``app/modules/remote/tunnel.py`` (remote-control tunnel). There are exactly
three, verified with ``grep -rn "validate_token" app/``; the agent gateway is
not a fourth (it identifies agents by TLS peer-certificate fingerprint and
carries no bearer token). An allowlist enforced at one of three doors is not
an allowlist.

WHY A REFUSAL IS AUDITED ONCE AND LOGGED TWICE, AND WHY THAT ASYMMETRY IS THE
HONEST ANSWER RATHER THAN AN OVERSIGHT
======================================================================

Invariant 4 wants every privileged action in the immutable, hash-chained audit
log, and a refused admission has a strong claim to be the entry most worth
having there: it is the observable trace of a credential being used after the
identity behind it stopped being allowed to use it. A logging WARNING is not a
substitute — ``app/core/logging.py`` says so itself in its first paragraph.
Operational lines are rotated by the log driver, unordered across processes,
and carry no integrity guarantee; an attacker who reaches the host can edit
them, and nothing detects it. That is exactly the property the chain exists to
supply. So the question was asked, and it has two different answers depending
on which of the two rules did the refusing.

**The ``users.status`` refusal IS auditable, and this module supplies the
means** (:func:`audit_admission_refusal`). It fires after the org has been
resolved from the token's slug and after the ``users`` row has been read, so
both ``org_id`` and ``user_id`` are in hand and the event is a normal,
tenant-scoped append written through the same ``org_session`` the door already
holds open for the status read.

**The domain refusal CANNOT be, and no wording makes it possible.** It fires
BEFORE the org lookup — deliberately, so that a refused identity does not
leave a self-healed ``users`` row behind for attack traffic to accumulate —
and ``audit_events.org_id`` is NOT NULL, RLS-scoped, and the value the chain
is linearised on: each event hashes the previous event *of that org*. There is
no org to write against and no chain to append to. The alternatives were
considered and are worse: resolving the org first in order to audit would
reintroduce the row-creation the door was ordered to avoid, and a
cross-tenant "system" chain would be a second, unlinked audit surface invented
for one event type.

**THEREFORE: THE ABSENCE OF ADMISSION-REFUSAL EVENTS IN THE CHAIN IS NOT
EVIDENCE THAT NOBODY WAS REFUSED.** That sentence is the reason this section
is long. A partial guarantee that reads as a total one is more dangerous than
a stated gap, and an operator reconstructing a §3.3 lockout from the audit log
alone would otherwise conclude the gate never fired. Domain refusals exist
ONLY in :func:`log_admission_refusal`'s operational line, and that is where
they have to be looked for.

The cost of auditing the half that can be audited, named rather than skipped:
a genuine token belonging to a disabled or deleted user, replayed in a loop,
appends to that org's chain at request rate, and each append takes the per-org
advisory lock. That is real, and it is accepted — the population able to do it
is one already-known, already-refused account per token, which is precisely
the activity worth having a tamper-evident record of. If it ever needs a
brake, the brake is rate-limiting the door, not dropping the evidence.
"""

import logging
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import N_
from app.core.identity.email_domain import domain_refusal, is_allowed_domain

logger = logging.getLogger("its.identity")

# The only `users.status` value that authorizes anything.
ACTIVE_STATUS = "active"

_STATUS_REFUSED = N_("user account is not active")


def status_refusal(status: str) -> str | None:
    """``None`` when the account may act, otherwise the refusal reason.

    Closes spec §0.4: ``users.status`` defaults to ``active`` and, before this,
    no code path in ``app/`` consulted it -- so a "disabled" user was fully
    authorized. It also closes the window in which a disabled user's already
    issued, not-yet-expired token keeps working, which no amount of Keycloak
    disabling can close on its own.

    ANYTHING that is not exactly ``active`` is refused. Migration 0065
    constrains the column to ``active``/``disabled``/``deleted``, but this does
    not lean on that constraint holding: a fourth value introduced later is
    refused until someone decides it should not be, rather than admitted
    because nobody remembered this function.

    The value is compared exactly, not case-folded -- ``"ACTIVE"`` is not a
    status this platform writes, so it is a corruption, and a corrupt status
    should fail closed.

    THE WRITE PATH SHIPS SEPARATELY. Setting a user to ``disabled`` or
    ``deleted`` is the user-lifecycle surface (spec §2). This is the read side,
    and it is deliberately landed first: enforcement that arrives after the
    button that claims to disable people is enforcement nobody had.
    """
    return None if status == ACTIVE_STATUS else _STATUS_REFUSED


def log_admission_refusal(door: str, *, sub: str, email: str, reason: str) -> None:
    """Record one refusal. THE ONLY LOG CALL SITE ON THIS PATH, deliberately.

    §3.3's danger is that switching the allowlist on locks out the operator
    who switched it on, and a 403 with no server-side trace of who was turned
    away or why is a lockout with nothing to debug. So refusals are logged --
    once, here, rather than six times across three doors, because the doors
    would drift apart in wording and in what they included and this is a
    security event that wants one shape.

    The address IS included, and that is a considered call rather than an
    oversight. It is not a credential (invariant 3 governs credentials), the
    same value already reaches the audit log in `auth.login`'s metadata, and
    the §3.3 preflight prints exactly this column -- an operator diagnosing
    their own lockout needs to match the two. The static template plus `extra`
    keeps it inside the logging kernel's redaction gate rather than
    interpolated past it.
    """
    logger.warning(
        "identity admission refused",
        extra={"door": door, "sub": sub, "email": email, "reason": reason},
    )


ADMISSION_REFUSED_ACTION: Final = "auth.admission_refused"


async def audit_admission_refusal(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    door: str,
    reason: str,
) -> None:
    """Append one refusal to the org's hash-chained audit log.

    For the ``users.status`` refusal only. See this module's docstring for the
    accounting: the domain refusal happens before an org exists, and the chain
    is linearised per org, so it has nowhere to be written.

    THE SESSION IS THE CALLER'S, AND IT MUST BE A TENANT SESSION. The door
    already holds an ``org_session`` open to read ``users.status``; the event
    is appended inside it so the write is constrained by RLS in the database
    rather than by application code (invariant 5), and so it commits or rolls
    back with the read that justified it.

    The actor is the refused user, not "system": they are who acted, and the
    request was theirs. What did not happen is the authorization, and that is
    what ``action`` says.

    ``reason`` and ``door`` go in the metadata, not the action name, so the
    chain does not grow a new action code every time a refusal rule is added
    and so an operator can group all refusals with one predicate. The address
    is NOT carried here: the event resolves to ``user_id``, whose row holds the
    address, and duplicating an identity fact into immutable storage that
    cannot later be corrected is a liability rather than a convenience. The
    operational line from :func:`log_admission_refusal` is where the
    address-as-seen-in-the-claim lives, which is the value the §3.3 preflight
    output has to be matched against.
    """
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=user_id,
        action=ADMISSION_REFUSED_ACTION,
        target_type="user",
        target_id=str(user_id),
        metadata={"door": door, "reason": reason},
    )


__all__ = [
    "ACTIVE_STATUS",
    "ADMISSION_REFUSED_ACTION",
    "audit_admission_refusal",
    "domain_refusal",
    "is_allowed_domain",
    "log_admission_refusal",
    "status_refusal",
]

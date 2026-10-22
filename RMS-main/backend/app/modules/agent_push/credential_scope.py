"""`push_credential_scope` and the `push_install` credential type
(spec §6.2, §6.3, §9; `CLAUDE.md` invariant 2a (d), invariant #3).

**THE CREDENTIAL ITSELF CARRIES WHERE IT MAY BE PRESENTED.** That sentence is
the module's answer to *how does one stored secret not get sprayed at the whole
estate*, and this file is where it is true. `credentials.device_group_id`
cannot express it: that column scopes a credential to DEVICES, and a push
target is not yet a managed device and may be in no group at all.

**NOTHING HERE DECRYPTS ANYTHING, AND IN CYCLE B1 NOTHING IN THIS MODULE
DOES.** A credential is resolved by id and by METADATA -- its org, its name,
its type -- and the plaintext is never fetched, because there is nothing yet
for it to be fetched FOR (spec §14: B1 "ends with a feature that refuses
everything, correctly, and dispatches nothing"). Invariant #3 says a stored
credential is decrypted only at point of use; in B1 there is no point of use,
so building a checkout path here would be creating the exposure a cycle before
anything needs it. `tests/test_agent_push_dispatches_nothing.py` asserts that
structurally -- no `vault`, no `checkout`, no `ciphertext` anywhere under
`app/modules/agent_push/`.

**THE TYPE IS DEDICATED, AND THE REFUSAL RUNS IN BOTH DIRECTIONS** (spec §6.2).
A `push_install` credential may not be used by remote control, and a
remote-control or provisioning credential may not be used here. Reuse means one
theft costs every subsystem at once and makes rotation impossible to reason
about. The push side is enforced here on every read path that resolves a
credential; the reverse is enforced in `app/modules/remote/service.py`, which
is the only other place in this repository that presents a stored credential to
a machine.

**WHAT THIS MODULE CANNOT ENFORCE, SAID PLAINLY** (spec §6.2, §1.3 item 2). It
cannot check what rights the account actually holds in somebody else's
directory. A `push_install` credential that is a Domain Admin satisfies every
rule in this file and defeats every bound in §5. The platform can refuse to
pretend the choice does not matter -- the console says so at the point of
creation -- and it cannot do more than that.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import Credential
from app.modules.agent_push.errors import (
    PushConflictError,
    PushNotFoundError,
    PushValidationError,
)
from app.modules.agent_push.models import PushCredentialScope
from app.modules.agent_push.net import parse_prefix, prefix_size

__all__ = [
    "PUSHABLE_CREDENTIAL_TYPES",
    "PUSH_CREDENTIAL_TYPE",
    "WINDOWS_DOMAIN_CREDENTIAL_TYPE",
    "bind_scope",
    "refuse_push_install_credential",
    "list_scopes",
    "resolve_push_credential",
    "unbind_scope",
]

#: The one credential type a push over SSH may use.
PUSH_CREDENTIAL_TYPE = "push_install"
#: The credential type a push over WinRM uses: a Windows domain account
#: (design §2.5). DOMAIN\user or a UPN, a password, and an optional explicit KDC.
WINDOWS_DOMAIN_CREDENTIAL_TYPE = "windows_domain"
#: Either may be pushed with; neither may be used by remote control or
#: provisioning, and theirs may not be used here (spec §6.2, dedication).
PUSHABLE_CREDENTIAL_TYPES = frozenset({PUSH_CREDENTIAL_TYPE, WINDOWS_DOMAIN_CREDENTIAL_TYPE})

_MAX_NOTE = 500


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def resolve_push_credential(
    session: AsyncSession, org_id: UUID, credential_id: UUID
) -> Credential:
    """The credential row, by id and METADATA ONLY -- never its plaintext.

    Two refusals, and they are deliberately different:

    * NOT IN THIS ORG -> `PushNotFoundError`, which the API maps to 404 and
      never 403. A 403 would confirm that the id exists somewhere, which is a
      cross-tenant existence oracle over the vault's own key space.
    * WRONG TYPE -> `PushValidationError` naming the type it found. This is
      spec §6.2's dedication rule, and it is checked on EVERY path that
      resolves a credential for a push rather than only at bind time: a
      credential whose type was edited after its scopes were bound must stop
      working immediately, not at the next bind.
    """
    credential = (
        await session.execute(
            select(Credential).where(
                Credential.id == credential_id, Credential.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if credential is None:
        raise PushNotFoundError(
            tr("credential {credential_id} not found", credential_id=credential_id)
        )
    if credential.type not in PUSHABLE_CREDENTIAL_TYPES:
        raise PushValidationError(
            tr(
                "credential {credential_id} is of type {credential_type}; a push may only "
                "use a credential of type {pushable_credential_types}. The credential is "
                "dedicated on purpose (spec §6.2): shared with remote control or "
                "provisioning, one theft costs every subsystem at once and rotation "
                "becomes impossible to reason about",
                credential_id=credential_id,
                credential_type=repr(credential.type),
                pushable_credential_types=sorted(PUSHABLE_CREDENTIAL_TYPES),
            )
        )
    return credential


async def bind_scope(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID | None,
    credential_id: UUID,
    prefix: str,
    note: str = "",
) -> PushCredentialScope:
    """Bind `credential_id` to `prefix`. HIGH PRIVILEGE.

    Gated on `agent.push.scope.manage`. Widening where a secret may be
    presented is an admin-tier, audited act -- it is one of the four
    independent bounds in spec §5.1, and there is no graceful degradation
    between "25 machines inside one authorized prefix" and "every machine the
    credential can authenticate to".
    """
    if actor_id is None:
        raise PushValidationError(
            tr(
                "bound_by is required: there is no system-bound credential scope. A human "
                "with agent.push.scope.manage decides where a secret may be presented"
            )
        )
    network = parse_prefix(prefix)
    if not isinstance(note, str) or len(note) > _MAX_NOTE:
        raise PushValidationError(
            tr("note must be a string of at most {max_note} characters", max_note=_MAX_NOTE)
        )
    credential = await resolve_push_credential(session, org_id, credential_id)

    now = _utcnow()
    inserted_id = (
        await session.execute(
            pg_insert(PushCredentialScope)
            .values(
                org_id=org_id,
                credential_id=credential_id,
                prefix=str(network),
                note=note,
                bound_by=actor_id,
                bound_at=now,
            )
            # INFERENCE against the PARTIAL index; see `authorization.py`.
            .on_conflict_do_nothing(
                index_elements=["org_id", "credential_id", "prefix"],
                index_where=text("unbound_at IS NULL"),
            )
            .returning(PushCredentialScope.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise PushConflictError(
            tr(
                "credential {credential_name} is already bound to {network}",
                credential_name=repr(credential.name),
                network=network,
            )
        )

    row = (
        await session.execute(
            select(PushCredentialScope).where(
                PushCredentialScope.org_id == org_id,
                PushCredentialScope.id == inserted_id,
            )
        )
    ).scalar_one()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="push.credential_scope_bound",
        target_type="push_credential_scope",
        target_id=str(row.id),
        metadata={
            "credential_id": str(credential_id),
            # THE NAME, NEVER THE SECRET, AND NEVER THE `vault_ref`. Invariant
            # #3: a credential is never written to a log or an audit metadata
            # dict. What an auditor needs to answer "which credential" is the
            # id and the label a human chose.
            "credential_name": credential.name,
            "prefix": str(network),
            "address_count": prefix_size(network),
            "note": note,
        },
    )
    await session.flush()
    return row


async def unbind_scope(
    session: AsyncSession, org_id: UUID, *, actor_id: UUID | None, scope_id: UUID
) -> None:
    """Stamp `unbound_at`/`unbound_by`. THE ROW STAYS.

    "Which addresses was this credential bound to when it was used in March"
    must stay answerable after somebody narrows the scope, which a DELETE
    would destroy.
    """
    row = (
        await session.execute(
            select(PushCredentialScope).where(
                PushCredentialScope.org_id == org_id,
                PushCredentialScope.id == scope_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise PushNotFoundError(tr("push credential scope {scope_id} not found", scope_id=scope_id))
    if row.unbound_at is not None:
        raise PushConflictError(
            tr("push credential scope {scope_id} is already unbound", scope_id=scope_id)
        )
    row.unbound_at = _utcnow()
    row.unbound_by = actor_id
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="push.credential_scope_unbound",
        target_type="push_credential_scope",
        target_id=str(row.id),
        metadata={
            "credential_id": str(row.credential_id),
            "prefix": str(row.prefix),
            "bound_by": str(row.bound_by),
        },
    )
    await session.flush()


async def list_scopes(
    session: AsyncSession,
    org_id: UUID,
    *,
    credential_id: UUID | None = None,
    include_unbound: bool = False,
) -> list[PushCredentialScope]:
    """Every prefix a credential may be presented in, or all of them.

    `credential_id` is OPTIONAL because the two questions are different: the
    credential's own page asks "where may this secret go", and the module's
    page asks "which secrets may go anywhere at all". A single-credential-only
    signature would make the second question require a query per credential and
    an operator to assemble the answer themselves.
    """
    query = select(PushCredentialScope).where(PushCredentialScope.org_id == org_id)
    if credential_id is not None:
        query = query.where(PushCredentialScope.credential_id == credential_id)
    if not include_unbound:
        query = query.where(PushCredentialScope.unbound_at.is_(None))
    return list(
        (
            await session.execute(
                query.order_by(
                    PushCredentialScope.credential_id, PushCredentialScope.prefix
                )
            )
        )
        .scalars()
        .all()
    )


async def refuse_push_install_credential(
    session: AsyncSession, *, org_id: UUID, credential_id: UUID
) -> None:
    """THE MIRROR OF `resolve_push_credential`, for everybody else (spec §6.2).

    A `push_install` or `windows_domain` credential may not be presented by
    remote control or by provisioning. `resolve_push_credential` above enforces
    one direction -- only a pushable credential may be pushed with -- and this
    enforces the other, from the module that owns the types rather than from a
    second copy of the constants somewhere else.

    **DENIES THE PUSH-DEDICATED TYPES, RATHER THAN ALLOWING A LIST.** An
    allowlist here would be this module deciding which credential types remote
    control may use, which is not its decision to make and would break the day
    somebody adds a type. Denying `PUSHABLE_CREDENTIAL_TYPES` is the whole rule
    and the whole change.

    Raises `PushValidationError`; the caller maps it. A credential that is not
    in this org, or does not exist, is NOT this function's business -- the
    caller's own lookup and the vault's own `org_id` predicate answer that,
    and raising here on a missing row would turn a 404 into a confusing 400.
    """
    found = (
        await session.execute(
            select(Credential.type).where(
                Credential.id == credential_id, Credential.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if found in PUSHABLE_CREDENTIAL_TYPES:
        raise PushValidationError(
            tr(
                "credential {credential_id} is dedicated to agent push install and may not "
                "be used for any other purpose (spec §6.2). It authenticates to machines "
                "nobody has enrolled, over a transport that cannot authenticate the target "
                "on first contact; sharing it means one theft costs every subsystem at once",
                credential_id=credential_id,
            )
        )

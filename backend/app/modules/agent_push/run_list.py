"""A subnet, expanded into the named machines a person confirms
(docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md §1.2, §1.3;
CLAUDE.md 2a(d), 2a(f) as amended 2026-09-30).

**A SUBNET BUILDS THE LIST; IT IS NEVER THE THING CONFIRMED.** The subnet must
be one of the organization's active push authorizations, exactly as
authorized. It expands to the vouched-for, approved, unenrolled machines whose
address it contains. Each is evaluated by the SAME ladder a ticked push runs
(`preflight.evaluate_targets`) and the SAME install-only checks
(`dispatch.pre_dispatch_checks`). A machine that fails any of them is listed
as blocked, with every reason, and is not part of what is confirmed.

**THE DIGEST IS OVER THE READY PAIRS** (R7). What a person confirms is "install
on these machines". A blocked machine that turns ready between showing and
confirming must change the digest, or they would have confirmed a machine
they were told would not be touched. **AND OVER THE CLIENT AND THE
CREDENTIAL** (plan 2026-10-26 Task 12; 2a(f) "bound by a digest of that
list"): a confirmation shown for one credential is not a confirmation for
another, of the same client or of another one, however alike the lists.

**ONE CLIENT'S SUBNET** (Task 12). A run is made in its credential's client:
the subnet must be one of THAT client's active authorizations, and the list
is that client's vouched machines in it -- another client's machines at the
same addresses are not candidates, and not counted.

**READS ONLY.** Nothing here writes a row, decrypts a secret or signs
anything, and `expand_prefix` takes no vault. Every query carries an explicit
`org_id` predicate in addition to RLS.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import N_, tr, tr_marked
from app.core.rbac import codes_held_anywhere, has_permission
from app.modules.agent_push.credential_scope import (
    credential_client,
    resolve_push_credential,
)
from app.modules.agent_push.dispatch import pre_dispatch_checks
from app.modules.agent_push.errors import PushPermissionError, PushValidationError
from app.modules.agent_push.models import PushAuthorization
from app.modules.agent_push.net import IPAddress, parse_prefix, parse_target_address
from app.modules.agent_push.preflight import VOUCHED_PROVENANCE, TargetDecision, evaluate_targets

__all__ = [
    "LIST_REASONS",
    "PUSH_RUN_LIST_MAX",
    "PUSH_RUN_MAX",
    "RunList",
    "RunListRow",
    "authorized_prefixes",
    "compute_list_digest",
    "expand_prefix",
]

#: CLAUDE.md 2a(f) as amended 2026-09-30: at most 1,024 named targets per run.
#: It counts READY machines, not the prefix's size: a /16 whose vouched-for,
#: ready machines number 300 is fine (design §1.2).
PUSH_RUN_MAX = 1024

#: How many vouched-for candidates one expansion evaluates (R13). A list longer
#: than this is not something a person reads, and each candidate costs the
#: ladder's queries. It is refused with the same advice.
PUSH_RUN_LIST_MAX = 4096

#: The one blocking reason expansion adds to the ladder's and dispatch's.
LIST_REASONS: dict[str, str] = {
    "not_installable": (
        N_(
            "the machine's operating system or processor is not recorded, or is not one "
            "push can install onto. Set its push install details on the machine's page"
        )
    ),
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RunListRow:
    """One listed machine. `reasons` empty means ready."""

    device_id: UUID
    address: str
    hostname: str
    os_family: str
    reasons: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.reasons


def compute_list_digest(
    pairs: Iterable[tuple[UUID, str]], *, client_id: UUID, credential_id: UUID
) -> str:
    """SHA-256 over `client:<client_id>` and `credential:<credential_id>`,
    then the sorted `(device_id, address)` pairs, one `<device_id>|<address>`
    line each: every line joined by a newline, UTF-8, lowercase hex. Only the
    server computes it; the console echoes it back."""
    lines = [f"client:{client_id}", f"credential:{credential_id}"]
    lines.extend(sorted(f"{device_id}|{address}" for device_id, address in pairs))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunList:
    prefix: str
    #: The run's client: its credential's (Task 12).
    client_id: UUID
    credential_id: UUID
    credential_name: str
    rows: tuple[RunListRow, ...]
    #: Rows in the subnet that nobody vouched for (a provenance outside
    #: `VOUCHED_PROVENANCE`, today a sweep's): never listed. Remedy: claim them.
    unvouched_count: int
    #: Rows somebody vouched for that are still waiting for review: never
    #: listed. Remedy: approve them.
    awaiting_review_count: int
    #: Addresses the ladder answered `ambiguous_asset` for (research-4 §I4):
    #: never listed, never in the digest, and counted rather than dropped in
    #: silence. Always 0 while the ladder is asked for one client, as it is.
    ambiguous_count: int = 0

    @property
    def ready_rows(self) -> tuple[RunListRow, ...]:
        return tuple(row for row in self.rows if row.ready)

    @property
    def list_digest(self) -> str:
        return compute_list_digest(
            ((row.device_id, row.address) for row in self.ready_rows),
            client_id=self.client_id,
            credential_id=self.credential_id,
        )


_CHECKOUT_REQUIRED = N_(
    "a subnet push requires the `credentials.checkout` permission as well as "
    "`agent.push.run`: it presents a stored secret to every machine it lists"
)


async def _checkout_anywhere(session: AsyncSession, actor_id: UUID) -> None:
    """The conjunction's first half: `credentials.checkout` in SOME client,
    refused before anything is read for a caller who may decrypt nothing
    anywhere."""
    if "credentials.checkout" not in await codes_held_anywhere(session, actor_id):
        raise PushPermissionError(tr_marked(_CHECKOUT_REQUIRED))


async def _checkout_in(session: AsyncSession, actor_id: UUID, client_id: UUID) -> None:
    """The conjunction in the run's client -- the credential's (Task 12): a
    technician holding both codes on one client builds runs there."""
    if not await has_permission(session, actor_id, "credentials.checkout", client_id=client_id):
        raise PushPermissionError(tr_marked(_CHECKOUT_REQUIRED))


async def authorized_prefixes(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID,
    credential_id: UUID | None = None,
    now: datetime | None = None,
) -> list[PushAuthorization]:
    """The subnets a run may be built from: ACTIVE push authorizations.
    Nothing else may be chosen, and nothing may be typed.

    With `credential_id`, the authorizations of THAT credential's client --
    the only subnets a run with it may be built from (Task 12) -- under the
    `credentials.checkout` conjunction in that client. Without one, every
    client's this session sees, and the conjunction must hold in every client.

    Refuses (`PushPermissionError`) a caller without `credentials.checkout`,
    the same conjunction every use of this list requires (`expand_prefix`,
    `create_run`): someone who may not build a run is not shown where one may
    be built."""
    stamp = now or _utcnow()
    query = select(PushAuthorization).where(
        PushAuthorization.org_id == org_id,
        PushAuthorization.revoked_at.is_(None),
        or_(PushAuthorization.expires_at.is_(None), PushAuthorization.expires_at > stamp),
    )
    if credential_id is None:
        if not await has_permission(session, actor_id, "credentials.checkout"):
            raise PushPermissionError(tr_marked(_CHECKOUT_REQUIRED))
    else:
        await _checkout_anywhere(session, actor_id)
        credential = await resolve_push_credential(session, org_id, credential_id)
        client_id = credential_client(credential)
        await _checkout_in(session, actor_id, client_id)
        query = query.where(PushAuthorization.client_id == client_id)
    rows = await session.execute(query.order_by(PushAuthorization.prefix))
    return list(rows.scalars())


async def _is_authorized_prefix(
    session: AsyncSession, org_id: UUID, client_id: UUID, prefix: str, now: datetime
) -> bool:
    row = (
        await session.execute(
            text(
                "SELECT 1 FROM push_authorization"
                " WHERE org_id = :org AND client_id = :client AND revoked_at IS NULL"
                "   AND (expires_at IS NULL OR expires_at > :now)"
                "   AND prefix = CAST(:prefix AS cidr)"
                " LIMIT 1"
            ),
            {"org": str(org_id), "client": str(client_id), "now": now, "prefix": prefix},
        )
    ).first()
    return row is not None


async def _candidate_addresses(
    session: AsyncSession, org_id: UUID, client_id: UUID, prefix: str
) -> list[IPAddress]:
    """Every address inside `prefix` that a vouched-for, approved row OF THE
    RUN'S CLIENT holds (CLAUDE.md 2a(d), R5; Task 12). Containment is decided
    by Postgres in native `inet`."""
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT primary_ip FROM devices"
                " WHERE org_id = :org AND client_id = :client"
                "   AND primary_ip <<= CAST(:prefix AS cidr)"
                "   AND provenance = ANY(CAST(:vouched AS text[]))"
                "   AND review_state = 'approved'"
                " ORDER BY primary_ip"
            ),
            {
                "org": str(org_id),
                "client": str(client_id),
                "prefix": prefix,
                "vouched": list(VOUCHED_PROVENANCE),
            },
        )
    ).all()
    return [parse_target_address(str(row.primary_ip)) for row in rows]


async def _unlisted_counts(
    session: AsyncSession, org_id: UUID, client_id: UUID, prefix: str
) -> tuple[int, int]:
    """`(unvouched, awaiting review)`: the run's client's rows inside
    `prefix` that are never listed, only counted, each with its own remedy
    (design §1.2; review item 6). Another client's rows are not this run's to
    count (Task 12).

    * UNVOUCHED is every provenance OUTSIDE the allowlist, not `= 'sweep'`:
      anything added to `devices.provenance` later is refused by silence
      (CLAUDE.md 2a(d)), and must be counted the same way rather than vanish.
    * AWAITING REVIEW is a vouched-for row whose `review_state` is `pending`.

    Neither counts a machine already running the agent (it needs no push, so
    claiming or approving it would not list it) nor a `rejected` tombstone,
    which every list hides."""
    row = (
        await session.execute(
            text(
                "SELECT"
                "   count(*) FILTER (WHERE provenance IS NULL"
                "     OR NOT (provenance = ANY(CAST(:vouched AS text[])))) AS unvouched,"
                "   count(*) FILTER (WHERE provenance = ANY(CAST(:vouched AS text[]))"
                "     AND review_state = 'pending') AS awaiting_review"
                " FROM devices d"
                " WHERE d.org_id = :org AND d.client_id = :client"
                "   AND d.primary_ip <<= CAST(:prefix AS cidr)"
                "   AND d.review_state <> 'rejected'"
                "   AND NOT EXISTS (SELECT 1 FROM agents a"
                "                   WHERE a.org_id = d.org_id AND a.device_id = d.id)"
            ),
            {
                "org": str(org_id),
                "client": str(client_id),
                "prefix": prefix,
                "vouched": list(VOUCHED_PROVENANCE),
            },
        )
    ).one()
    return int(row.unvouched), int(row.awaiting_review)


async def _enrolled(session: AsyncSession, org_id: UUID, device_ids: list[UUID]) -> set[UUID]:
    if not device_ids:
        return set()
    rows = (
        await session.execute(
            text(
                "SELECT device_id FROM agents"
                " WHERE org_id = :org AND device_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"org": str(org_id), "ids": [str(i) for i in device_ids]},
        )
    ).all()
    return {UUID(str(row.device_id)) for row in rows}


async def _device_facts(
    session: AsyncSession, org_id: UUID, device_ids: list[UUID]
) -> dict[UUID, tuple[str, str]]:
    if not device_ids:
        return {}
    rows = (
        await session.execute(
            text(
                "SELECT id, hostname, os_family FROM devices"
                " WHERE org_id = :org AND id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"org": str(org_id), "ids": [str(i) for i in device_ids]},
        )
    ).all()
    return {UUID(str(row.id)): (row.hostname or "", row.os_family or "") for row in rows}


async def expand_prefix(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID,
    prefix: str,
    credential_id: UUID,
    public_console_url: str | None,
    now: datetime | None = None,
) -> RunList:
    """The list for `prefix` and `credential_id`, as a person will be shown it.

    Raises, in this order:
    * `PushPermissionError` when the caller holds `credentials.checkout` in no
      client. This is the same conjunction `request_push_batch` checks first,
      because a caller who may not use a stored secret must not be told which
      machines it would reach;
    * `PushNotFoundError` / `PushValidationError` / `PushClientError` for a
      credential that is not this org's (or not visible), not a push
      credential, or of no client;
    * `PushPermissionError` when the caller does not hold
      `credentials.checkout` in the credential's client, the run's;
    * `PushValidationError` for a prefix that is not one of that client's
      active authorizations exactly as authorized, a list longer than
      `PUSH_RUN_LIST_MAX`, or more than `PUSH_RUN_MAX` ready machines.
    """
    await _checkout_anywhere(session, actor_id)
    stamp = now or _utcnow()
    canonical = str(parse_prefix(prefix))
    credential = await resolve_push_credential(session, org_id, credential_id)
    client_id = credential_client(credential)
    await _checkout_in(session, actor_id, client_id)
    if not await _is_authorized_prefix(session, org_id, client_id, canonical, stamp):
        raise PushValidationError(
            tr(
                "{canonical} is not one of this client's active push authorizations. "
                "A subnet push is built from one of those, exactly as authorized; a holder "
                "of agent.push.scope.manage authorizes a prefix",
                canonical=canonical,
            )
        )
    addresses = await _candidate_addresses(session, org_id, client_id, canonical)
    if len(addresses) > PUSH_RUN_LIST_MAX:
        raise PushValidationError(
            tr(
                "{canonical} holds {addresses} machines somebody vouched for, more than "
                "the {push_run_list_max} one list can show a person. Choose a narrower "
                "subnet",
                canonical=canonical,
                addresses=len(addresses),
                push_run_list_max=PUSH_RUN_LIST_MAX,
            )
        )
    decisions = await evaluate_targets(
        session,
        org_id,
        addresses=addresses,
        credential_id=credential_id,
        client_id=client_id,
        now=stamp,
    )
    candidates: list[tuple[TargetDecision, UUID]] = []
    ambiguous = 0
    for decision in decisions:
        if decision.device_id is not None:
            candidates.append((decision, decision.device_id))
        elif "ambiguous_asset" in decision.refusals:
            # Counted, never listed (research-4 §I4): an address that does not
            # name one machine is not a machine a person can confirm.
            ambiguous += 1
    device_ids = [device_id for _decision, device_id in candidates]
    enrolled = await _enrolled(session, org_id, device_ids)
    facts = await _device_facts(session, org_id, device_ids)

    rows: list[RunListRow] = []
    for decision, device_id in candidates:
        if device_id in enrolled:
            continue
        reasons = list(decision.refusals)
        try:
            checks = await pre_dispatch_checks(
                session,
                org_id,
                decisions=[decision],
                credential=credential,
                kind="install",
                public_console_url=public_console_url,
            )
        except PushValidationError:
            reasons.append("not_installable")
        else:
            reasons.extend(
                r for r in checks.refusals.get(decision.address, []) if r not in reasons
            )
        hostname, os_family = facts.get(device_id, ("", ""))
        rows.append(
            RunListRow(
                device_id=device_id,
                address=decision.address,
                hostname=hostname,
                os_family=os_family,
                reasons=tuple(reasons),
            )
        )
    rows.sort(key=lambda row: ip_address(row.address))

    ready = sum(1 for row in rows if row.ready)
    if ready > PUSH_RUN_MAX:
        raise PushValidationError(
            tr(
                "{canonical} expands to {ready} machines ready for a push; a run holds at "
                "most {push_run_max}. Choose a narrower subnet",
                canonical=canonical,
                ready=ready,
                push_run_max=PUSH_RUN_MAX,
            )
        )
    unvouched, awaiting_review = await _unlisted_counts(session, org_id, client_id, canonical)
    return RunList(
        prefix=canonical,
        client_id=client_id,
        credential_id=credential.id,
        credential_name=credential.name,
        rows=tuple(rows),
        unvouched_count=unvouched,
        awaiting_review_count=awaiting_review,
        ambiguous_count=ambiguous,
    )

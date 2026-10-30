"""The refusal ladder: the four bounds of invariant 2a, evaluated before
anything can be signed (spec §3, §5.1, §5.2, §9; `CLAUDE.md` invariant 2a).

**SECURITY-CRITICAL, AND IN CYCLE B1 THIS FILE IS THE WHOLE FEATURE.** Spec
§14 splits Part B in two precisely so the refusals can be found wrong while
nothing can reach a machine: B1 "ends with a feature that refuses everything,
correctly, and dispatches nothing". So `request_push_batch` runs the entire
ladder and then raises `PushDispatchUnavailableError` even when every bound is
satisfied. **There is no branch here that creates a job, builds a payload or
produces a signature, and there is no code path that could.**
`tests/test_agent_push_dispatches_nothing.py` asserts that from the AST and
from the module's imports rather than trusting this paragraph.

**THE REFUSAL IS ON THE NEAR SIDE OF THE SIGNING BOUNDARY, AND THAT IS THE
POINT OF EVALUATING IT HERE RATHER THAN AT DISPATCH.** `ipam/sweep.py` records
its refusal and raises BEFORE `JobEngine.create`, because revocation cannot
recall an already-signed in-flight job -- so the only safe place to refuse is
before there is a signature. This module inherits that structure verbatim, and
inherits it one privilege level up: `net.sweep`'s originator holds nothing,
while a push originator would hold a plaintext administrative credential in
process memory for the job's duration.

THE FOUR BOUNDS, and each is a different question:

  1. **`no_asset`** -- invariant 2a (d), first authorization. The target must
     be a `devices` row in this org whose `primary_ip` is the target address
     **AND WHOSE `provenance` IS NOT `sweep`**. The clause's words are "a
     promoted asset row", and since 2026-08-26 "a `devices` row exists" no
     longer means that -- see `VOUCHED_PROVENANCE` below, which is the most
     important comment in this file. **`credential_missing`** is its
     sibling: the address IS a vouched machine, of another client than the
     push's, so no credential of that machine's client was chosen.
     **`ambiguous_asset`** stays as a guard the ladder cannot reach: it is
     always asked for ONE client's machine (below), so the rows it reads are
     of one client.
  2. **`no_authorization`** -- (d), second. An ACTIVE `push_authorization`
     of the push's client must cover the address. Deny by default, and NOT
     implied by an `ipam_scan_authorization` over the same prefix (spec §3,
     D9), nor by another client's authorization of the same prefix.
  3. **`credential_not_scoped`** -- (d), third. The chosen credential must
     carry a `push_credential_scope` covering the address. The credential
     itself carries where it may be presented.
  4. **`no_site_server`** -- invariant 2a (a). The originator must be a device
     carrying an ENABLED `site_server` row whose segments contain the address.
     **There is no fallback**: not to another agent on the segment, not to a
     site server on a different segment, and above all not to the control
     plane. `provisioning/dispatch.py::_resolve_wake` made exactly this call
     for Wake-on-LAN -- "no site server serves the target's segment =>
     refusal, before the run is created" -- and the reasoning transfers
     verbatim, with more at stake: a `net.sweep` originator holds a list of
     addresses it could have observed anyway, and a push originator holds a
     plaintext administrative credential. `select_sweeper`'s "any agent with a
     current observation in the subnet" is REJECTED here for that reason and
     that reason alone (spec §2.1, D3). **AND THE SITE SERVER IS THE TARGET'S
     OWN CLIENT'S** (MSP clients, invariant 5a): the originator receives the
     decrypted credential, so a push to X's machine never goes through Y's
     site server -- not even for a technician who may act on every client,
     and not even when Y's declared segment is the more specific match.

**THE CLIENT OF A PUSH IS ITS CREDENTIAL'S** (MSP clients, plan 2026-10-26
Task 12: 2a(d) narrowed from the organisation to the client). A push
credential has exactly one client, and every bound is read inside it: the
machine, the authorization, the credential's scope and the originator are all
that client's. Two customers on one private range therefore never make an
address ambiguous: the credential says whose network the push is in. This
narrows 2a(d), never widens it -- a bound that held for the organisation must
now hold inside one client as well.

**EVERY FAILING REASON IS COLLECTED, NOT THE FIRST.** Spec §9: "a refusal that
does not say which is a refusal nobody can act on". An operator told only the
first of three missing grants comes back twice; one told "something was
refused" cannot act at all.

**VALIDATION IS ALL-OR-NOTHING; EXECUTION WOULD BE PER-TARGET** (spec §5.2).
If any address in the batch fails any bound, the WHOLE request is refused with
the offending addresses named, before anything is created. An operator must
not discover mid-batch that four of their twenty-five were outside the
authorization.

**EVERY QUERY CARRIES AN EXPLICIT `org_id` PREDICATE**, in addition to RLS.
Through `org_session` RLS rejects a cross-org row first, so deleting one of
these predicates turns no route test red;
`tests/test_agent_push_preflight.py` pins them on the BYPASSRLS admin engine,
where the application predicate is the only thing scoping anything.

**CONTAINMENT IS DECIDED BY POSTGRES, IN NATIVE `cidr`/`inet`.** `>>=` against
the stored type is correct for both address families with no second code path;
the same comparison in Python over `text` columns would be a second answer to
"is this address inside that prefix" and would be wrong at every boundary
(`10.200.7.5` starts with `10.20`).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import N_, tr, tr_marked
from app.modules.agent_push.checkout import require_checkout_in, require_checkout_somewhere
from app.modules.agent_push.credential_scope import (
    credential_client,
    resolve_push_credential,
)
from app.modules.agent_push.errors import (
    PushAuthorizationError,
    PushError,
    PushValidationError,
)
from app.modules.agent_push.net import IPAddress, parse_target_address
from app.modules.site_server.registry import list_site_servers

__all__ = [
    "PUSH_BATCH_MAX",
    "VOUCHED_PROVENANCE",
    "REFUSAL_REASONS",
    "WINRM_PUSH_CAPABILITY",
    "ENROLL_URL_PUSH_CAPABILITY",
    "transport_for_os_family",
    "PushBatchOutcome",
    "TargetDecision",
    "evaluate_targets",
    "request_push_batch",
]

#: Spec §5.1, D6. ONE OF FOUR INDEPENDENT BOUNDS, and the only one widened by a
#: code change rather than by an audited admin act -- deliberately, because it
#: is the bound that answers "how much can one mistaken click do" and a number
#: an operator can raise in the console is not a bound.
#:
#: 25 rather than 1. Strictly-one-per-request is what invariant 1b(f) reaches
#: for with remote erase and it was seriously considered; it is rejected here
#: because a rule nobody can follow gets worked around. An operator onboarding
#: 200 machines through 200 modals will instead find the credential and push
#: from a laptop with psexec, outside the platform and unaudited (spec §5.2).
PUSH_BATCH_MAX = 25

#: The named reasons, and the remedy each implies. A refusal an operator cannot
#: act on is one they will look for a way around.
REFUSAL_REASONS: dict[str, str] = {
    "no_asset": (
        N_(
            "no device record that a person stood behind has this address. A push "
            "target must be a record somebody promoted or typed in -- not a row the "
            "sweep recorded on its own, which is an ICMP echo reply and may be a "
            "printer, a phone or the coffee machine. Open the device record and "
            "promote it (assets.manage), which is how a human vouches for it"
        )
    ),
    "ambiguous_asset": (
        N_(
            "device records of more than one client, each vouched for by a person, hold "
            "this address, so it does not name one machine and nothing is chosen for you. "
            "A push is made in the client of its credential: inside that client the "
            "address names that client's machine, and only that client's site server may "
            "carry the credential to it. Choose a push credential of the machine's client"
        )
    ),
    "credential_missing": (
        N_(
            "the machine at this address belongs to another client than the chosen "
            "credential, and a credential is presented only to machines of its own client. "
            "Choose a push credential of the machine's client"
        )
    ),
    "no_authorization": (
        N_(
            "no active push authorization covers this address. Deny by default: "
            "authorizing an IPAM scan of the same prefix authorizes nothing here, "
            "because authorizing an ICMP echo is not authorizing an administrative "
            "login. A holder of agent.push.scope.manage must authorize the prefix"
        )
    ),
    "credential_not_scoped": (
        N_(
            "the chosen credential is not bound to a prefix containing this "
            "address. The credential carries where it may be presented; binding a "
            "new prefix requires agent.push.scope.manage"
        )
    ),
    "no_site_server": (
        N_(
            "no enabled site server serves this address's segment. Every packet "
            "that reaches an unenrolled machine must originate from a device the "
            "operator declared a site server for that machine's segment, and there "
            "is no fallback originator -- not another agent on the segment, and "
            "never the control plane. Promote a device on that segment to site "
            "server (site.manage), or install the agent from the install command"
        )
    ),
    "site_server_too_old": (
        N_(
            "the only site server serving this segment runs an agent too old to push "
            "install: it cannot tell the machine it installs where to enroll, so the "
            "agent it placed would never appear here. It is skipped rather than used. "
            "Upgrade it, or promote a newer device on that segment to site server "
            "(site.manage)"
        )
    ),
    "no_winrm_site_server": (
        N_(
            "the only site server serving this segment runs an agent too old to push "
            "over WinRM: it does not advertise agent.push_install.winrm, so it cannot "
            "reach a Windows target and is skipped rather than used (design §4). "
            "Upgrade it to 1.6.x, or promote a newer device on that segment to site "
            "server (site.manage)"
        )
    ),
}

#: The capability a site server's agent advertises when its `pushinstall`
#: speaks WinRM (design §4, plan §I6). It is not a job type -- WinRM is a
#: transport WITHIN `agent.push_install` -- which is exactly why it has to be
#: advertised separately: an agent older than 1.6.x advertises
#: `agent.push_install` too, and cannot push to Windows.
WINRM_PUSH_CAPABILITY = "agent.push_install.winrm"

#: The capability a site server's agent advertises when its SSH push hands the
#: placed binary its enroll URL (`its-agent install --enroll-url`, review Minor
#: 1). An agent without it runs the old command, which installs an agent that
#: can never enroll, so for an `ssh` target it is skipped exactly as a site
#: server without WINRM_PUSH_CAPABILITY is for a `winrm` one.
ENROLL_URL_PUSH_CAPABILITY = "agent.push_install.enroll_url"

#: The capability each transport requires of its originator.
_TRANSPORT_CAPABILITY = {
    "winrm": WINRM_PUSH_CAPABILITY,
    "ssh": ENROLL_URL_PUSH_CAPABILITY,
}


def transport_for_os_family(os_family: str | None) -> str:
    """THE OS -> TRANSPORT RULE, IN ONE PLACE (design §2.1): Windows over WinRM
    (Kerberos only), everything else over SSH.

    It lives here rather than in `dispatch.py` because the ladder is the first
    place the transport matters (which site server may originate), and
    `dispatch.py` imports this module, never the reverse. `dispatch._device_target`
    reads it from here.
    """
    return "winrm" if (os_family or "").strip().lower() == "windows" else "ssh"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class TargetDecision:
    """What the ladder found for one named address.

    `refusals` EMPTY means every bound in invariant 2a is satisfied for this
    address. It does not mean anything was dispatched: in cycle B1 nothing is,
    ever.
    """

    address: str
    #: The push's client -- its credential's -- in which every bound was read.
    #: Required: every audit and event of the address is filed under it.
    client_id: UUID
    device_id: UUID | None = None
    authorization_id: UUID | None = None
    credential_scope_id: UUID | None = None
    site_server_id: UUID | None = None
    site_server_device_id: UUID | None = None
    refusals: tuple[str, ...] = field(default=())

    @property
    def authorized(self) -> bool:
        return not self.refusals


def _validated_batch(targets: object) -> list[IPAddress]:
    """The batch, as named host addresses, or a refusal.

    THREE REFUSALS, EACH ITS OWN MESSAGE, because they are three different
    operator mistakes:

    * empty -- a push against nothing is a request nobody meant to make;
    * over `PUSH_BATCH_MAX` -- the ceiling is the safety property, and it is
      checked BEFORE the per-target work so a 4000-address paste costs one
      refusal rather than 4000 queries;
    * duplicated -- the same machine named twice is either a paste error or a
      list the operator did not actually read, and spec §10.4 requires the
      confirmation to list every address with no "and 22 more". A silently
      deduplicated batch would make the count on the screen disagree with the
      count in the request.
    """
    if not isinstance(targets, list | tuple):
        raise PushValidationError(tr("targets must be a list of IP address literals"))
    if not targets:
        raise PushValidationError(
            tr(
                "a push must name at least one target address (invariant 2a clause (f): "
                "explicitly named addresses a human selected)"
            )
        )
    if len(targets) > PUSH_BATCH_MAX:
        raise PushValidationError(
            tr(
                "a push may name at most {push_batch_max} addresses in one act (got "
                "{targets}). The ceiling is one of four independent bounds on what a "
                "single mistaken click can do (spec §5.1)",
                push_batch_max=PUSH_BATCH_MAX,
                targets=len(targets),
            )
        )
    parsed = [parse_target_address(value) for value in targets]
    seen: set[str] = set()
    duplicates: list[str] = []
    for address in parsed:
        literal = str(address)
        if literal in seen:
            duplicates.append(literal)
        seen.add(literal)
    if duplicates:
        raise PushValidationError(
            tr(
                "duplicate target addresses: {duplicates}. Every address in a push is "
                "confirmed individually by a human, so the same machine named twice is a "
                "list nobody read",
                duplicates=sorted(set(duplicates)),
            )
        )
    return parsed


#: The `devices.provenance` values that mean A PERSON STOOD BEHIND THIS ROW.
#:
#: **THIS SET IS WHY THE FIRST BOUND STILL MEANS ANYTHING, AND IT IS NEWER THAN
#: THE SPEC IT IMPLEMENTS.** `spec-push-install.md` §3 condition 1 says the
#: target must be "a `devices` row in this org", and it argues the case at
#: length: "an echo reply is not an asset -- a sweep on a corporate LAN returns
#: printers, phones, a colleague's laptop on the wrong VLAN and the coffee
#: machine … Promotion is the moment a human asserted *this is a machine we own
#: and manage*."
#:
#: On the SAME DAY invariant 2a was approved, the operator also ruled that a
#: completed `net.sweep` records a `devices` row for every host it reported,
#: with nobody selecting anything (`docs/modules/asset-inventory/spec.md` §3,
#: migration 0078). **That ruling silently defeats the sentence above.** After
#: it, "a `devices` row exists at 10.0.4.17" is exactly what §3 says it must not
#: be taken to mean: the coffee machine now HAS a row, created by the sweep, and
#: a check written to the spec's literal words would present administrative
#: credentials to it on the strength of its having answered a ping.
#:
#: What answers it is the column that ruling added for precisely this purpose.
#: `provenance` says who, if anyone, stood behind the row -- `agent`,
#: `operator`, `promoted` or `sweep` -- and `sweep` is the one value that means
#: NOBODY DID. So the bound is `provenance <> 'sweep'`, and the spec's INTENT is
#: preserved where its wording no longer is.
#:
#: **STATED AS AN ALLOWLIST, NOT AS `!= 'sweep'`.** A denylist admits the next
#: value somebody adds to that CHECK constraint by silence; this refuses it by
#: silence instead, which is the right direction for a bound on credentialed
#: logins. `agent` is included because a row created by an enrolled agent was
#: vouched for by an enrollment somebody authorized -- and a machine that is
#: already running the agent has no use for a push anyway.
#: AND NOT WAITING FOR REVIEW (install-link plan D9): a row a link enrollment
#: created carries provenance 'agent' but no person has vouched for it yet.
VOUCHED_PROVENANCE = ("agent", "operator", "promoted")


@dataclass(frozen=True)
class _Asset:
    """The vouched-for machine an address names in one client.

    `ambiguous` is the `ambiguous_asset` refusal: rows of more than one client
    hold the address, so `device_id` is `None` -- no machine is chosen, and so
    no site server either. Asked for one client's rows, as the ladder always
    asks, it cannot happen; it is kept as a guard, so a lookup that lost its
    client filter refuses instead of choosing by row id.
    """

    device_id: UUID | None = None
    ambiguous: bool = False


#: A vouched-for, approved machine: the first bound's predicate, written once
#: for the two lookups that read it.
_VOUCHED = (
    " WHERE org_id = :org AND primary_ip = CAST(:addr AS inet)"
    "   AND provenance = ANY(CAST(:vouched AS text[]))"
    "   AND review_state = 'approved'"
)


async def _vouched_asset(
    session: AsyncSession, org_id: UUID, address: IPAddress, client_id: UUID
) -> _Asset:
    """The vouched-for `devices` row of `client_id` at this address (spec §3,
    cond. 1).

    **THE PUSH'S CLIENT, IN THE QUERY.** Inside an MSP organization two
    customers can use the same private range, so one address may be held by
    machines of two clients. The push is made in its credential's client, and
    only that client's machine is a target -- by `client_id` here, not left to
    RLS: an every-client technician sees every client's machines. (The pacer
    runs each run in its own client's scope, `ClientScope.only([run.client_id])`
    in `worker.py`, and asks the same question the same way.) Never
    "the lowest id", which would push to whichever customer's machine
    happened to be recorded first.

    Within one client the rows are ORDERED, so two records of one machine (a
    re-image) cannot make this return a different one between runs.
    `devices.primary_ip` is `inet`, so this is an address comparison and not
    a string one.

    See `VOUCHED_PROVENANCE` for why the provenance predicate is here and why
    it is the load-bearing half of this query.
    """
    rows = (
        await session.execute(
            text("SELECT id, client_id FROM devices" + _VOUCHED + " AND client_id = :client"
                 " ORDER BY id"),
            {
                "org": str(org_id),
                "addr": str(address),
                "vouched": list(VOUCHED_PROVENANCE),
                "client": str(client_id),
            },
        )
    ).all()
    if not rows:
        return _Asset()
    if len({str(row.client_id) for row in rows}) > 1:
        return _Asset(ambiguous=True)
    return _Asset(device_id=UUID(str(rows[0].id)))


async def _held_by_another_client(
    session: AsyncSession, org_id: UUID, address: IPAddress, client_id: UUID
) -> bool:
    """Is the address a vouched machine of a client OTHER than the push's,
    among those this session sees? It only NAMES the refusal
    (`credential_missing` instead of `no_asset`); nothing is ever chosen from
    it."""
    row = (
        await session.execute(
            text("SELECT 1 FROM devices" + _VOUCHED + " AND client_id <> :client LIMIT 1"),
            {
                "org": str(org_id),
                "addr": str(address),
                "vouched": list(VOUCHED_PROVENANCE),
                "client": str(client_id),
            },
        )
    ).first()
    return row is not None


async def _asset_for(
    session: AsyncSession, org_id: UUID, address: IPAddress, client_id: UUID
) -> UUID | None:
    """The vouched-for machine of `client_id` at this address, or `None`
    (`_vouched_asset`)."""
    return (await _vouched_asset(session, org_id, address, client_id)).device_id


async def _authorization_for(
    session: AsyncSession, org_id: UUID, address: IPAddress, now: datetime, client_id: UUID
) -> UUID | None:
    """The most specific ACTIVE `push_authorization` OF `client_id` covering
    this address.

    FIVE PREDICATES, ALL LOAD-BEARING, and each has its own test:

    * `client_id` -- the push's client (Task 12): a prefix means something
      only inside one client's network, so another client's authorization of
      the same prefix authorizes nothing here;
    * `org_id` -- RLS rejects a cross-org row through `org_session`, and this
      predicate is what rejects it on the admin engine and everywhere else;
    * `revoked_at IS NULL` -- a revoked grant authorizes nothing, and a test
      that seeds NO row cannot tell "the revocation predicate works" from
      "this function refuses everything";
    * `expires_at` -- a lapsed onboarding window is the ordinary case;
    * `prefix >>= address` -- containment in native `cidr`.

    MOST SPECIFIC FIRST is not decoration: `masklen` ordering is what makes the
    recorded `authorization_id` the grant an auditor would name, rather than
    whichever of two overlapping rows happened to sort first.
    """
    row = (
        await session.execute(
            text(
                "SELECT id FROM push_authorization"
                " WHERE org_id = :org AND client_id = :client"
                "   AND revoked_at IS NULL"
                "   AND (expires_at IS NULL OR expires_at > :now)"
                "   AND prefix >>= CAST(:addr AS inet)"
                " ORDER BY masklen(prefix) DESC, id"
                " LIMIT 1"
            ),
            {"org": str(org_id), "client": str(client_id), "now": now, "addr": str(address)},
        )
    ).first()
    return None if row is None else UUID(str(row.id))


async def _credential_scope_for(
    session: AsyncSession, org_id: UUID, credential_id: UUID, address: IPAddress
) -> UUID | None:
    """The most specific ACTIVE scope binding THIS credential to this address.

    `credential_id` is a predicate rather than a post-filter, and it is the one
    that makes this bound mean anything: without it, any credential in the org
    being bound anywhere would satisfy every target.
    """
    row = (
        await session.execute(
            text(
                "SELECT id FROM push_credential_scope"
                " WHERE org_id = :org"
                "   AND credential_id = :cred"
                "   AND unbound_at IS NULL"
                "   AND prefix >>= CAST(:addr AS inet)"
                " ORDER BY masklen(prefix) DESC, id"
                " LIMIT 1"
            ),
            {"org": str(org_id), "cred": str(credential_id), "addr": str(address)},
        )
    ).first()
    return None if row is None else UUID(str(row.id))


async def _advertises(
    session: AsyncSession, org_id: UUID, device_id: UUID, capability: str
) -> bool:
    """Does the agent on this site-server device advertise `capability`?
    FAIL CLOSED: no agents row, an empty map, or anything but a literal `true`
    is "no" -- an agent is taken to be able to do something only when it has
    said so (design §4: an older site server is skipped, never used)."""
    row = (
        await session.execute(
            text(
                "SELECT capabilities FROM agents"
                " WHERE org_id = :org AND device_id = :dev"
            ),
            {"org": str(org_id), "dev": str(device_id)},
        )
    ).first()
    capabilities = None if row is None else row.capabilities
    return isinstance(capabilities, dict) and capabilities.get(capability) is True


async def _site_server_for(
    session: AsyncSession,
    org_id: UUID,
    address: IPAddress,
    *,
    transport: str | None,
    client_id: UUID,
) -> tuple[UUID, UUID] | None:
    """The enabled site server OF `client_id` whose declared segments contain
    this address, and -- for a `winrm` target -- whose agent advertises the
    WinRM push capability.

    **THE CLIENT IS A FILTER, AND IT IS THE PUSH'S** -- its credential's,
    whose machine is being pushed to (MSP clients, invariant 5a; Task 12).
    The originator receives the decrypted push credential, so it must be a
    machine of that client. Applied in the query, not left to RLS: a
    technician who holds every client sees every client's site servers, and
    one of another client declared for a MORE SPECIFIC segment would
    otherwise come first. `client_id` is REQUIRED, never "any client this
    session sees": every caller (`evaluate_targets`) passes the push's client.

    **`list_site_servers` IS REUSED RATHER THAN RE-QUERIED**, and that matters
    for a reason the site-server module already wrote down: containment makes
    the candidate set ambiguous, so the ORDER is part of the answer, and that
    ordering ("most specific matching segment first") lives in exactly one
    function. A second query here would be a second answer to "who serves this
    wire", diverging on the first bug fixed in one of them.

    **AN IPv6 TARGET HAS NO SITE SERVER, AND SAYS SO RATHER THAN RAISING.**
    `site_server.segments` is IPv4-only by construction -- `parse_segment`
    refuses IPv6 because a Wake-on-LAN broadcast has no IPv6 form -- so no
    declared segment can contain an IPv6 address. That is `no_site_server`,
    which is a first-class recorded outcome with a named remedy, not an
    exception an operator has to interpret.

    **LIVENESS IS NOT CHECKED HERE.** Whether the chosen site server's agent is
    online belongs to dispatch, which is cycle B2. Checking it in B1 would
    make an authorization decision depend on a machine's uptime.

    **FOR `winrm` THE CAPABILITY IS A FILTER, NOT A TIE-BREAK** (design §4,
    plan §I6). Candidates keep `list_site_servers`' most-specific-first order;
    the ones whose agent does not advertise `agent.push_install.winrm` are
    skipped, and the first capable one originates. No capable candidate is
    `None`, exactly as no candidate at all is -- there is no fallback to an
    agent that cannot speak the transport. **`ssh` FILTERS THE SAME WAY** on
    `agent.push_install.enroll_url` (review Minor 1): an older agent's SSH push
    installs an agent that never learns where to enroll. `transport=None`
    applies no filter -- it is how the ladder asks "was there a site server at
    all?" to name the refusal.
    """
    if address.version != 4:
        return None
    candidates = await list_site_servers(
        session, org_id, enabled=True, segment=f"{address}/32", client_id=client_id
    )
    required = None if transport is None else _TRANSPORT_CAPABILITY[transport]
    for candidate in candidates:
        if required is not None and not await _advertises(
            session, org_id, candidate.device_id, required
        ):
            continue
        return candidate.id, candidate.device_id
    return None


async def _asset_transport(
    session: AsyncSession, org_id: UUID, device_id: UUID | None
) -> str:
    """The transport a push to this asset would use, from its declared OS.

    An address with no vouched-for asset is already refused `no_asset`; it is
    evaluated as `ssh` so the site-server bound reports what it always did.
    """
    if device_id is None:
        return "ssh"
    row = (
        await session.execute(
            text("SELECT os_family FROM devices WHERE org_id = :org AND id = :id"),
            {"org": str(org_id), "id": str(device_id)},
        )
    ).first()
    return transport_for_os_family(None if row is None else row.os_family)


async def evaluate_targets(
    session: AsyncSession,
    org_id: UUID,
    *,
    addresses: list[IPAddress],
    credential_id: UUID,
    client_id: UUID,
    now: datetime | None = None,
) -> list[TargetDecision]:
    """Run all four bounds against every address, inside `client_id` -- the
    push's client, its credential's. Reads only; writes nothing.

    `client_id` is REQUIRED: a ladder that could be asked with no client would
    have to choose a machine among clients, and it never chooses.

    Deliberately separate from `request_push_batch` so the ladder can be tested
    directly, and so cycle B2's dispatcher calls the SAME function rather than
    growing a second, subtly different copy of the authorization logic. A
    second copy is how "the console said it would be refused" and "the
    dispatcher signed it anyway" come to both be true.
    """
    stamp = now or _utcnow()
    decisions: list[TargetDecision] = []
    for address in addresses:
        refusals: list[str] = []
        asset = await _vouched_asset(session, org_id, address, client_id)
        device_id = asset.device_id
        if asset.ambiguous:
            refusals.append("ambiguous_asset")
        elif device_id is None:
            if await _held_by_another_client(session, org_id, address, client_id):
                refusals.append("credential_missing")
            else:
                refusals.append("no_asset")
        authorization_id = await _authorization_for(session, org_id, address, stamp, client_id)
        if authorization_id is None:
            refusals.append("no_authorization")
        scope_id = await _credential_scope_for(
            session, org_id, credential_id, address
        )
        if scope_id is None:
            refusals.append("credential_not_scoped")
        transport = await _asset_transport(session, org_id, device_id)
        # THE PUSH'S CLIENT CHOOSES THE ORIGINATOR (invariant 5a): the site
        # server is always looked for in it, so the refusal names every
        # missing bound -- but no originator is taken for an address with no
        # machine of that client: it is already refused.
        originator = await _site_server_for(
            session, org_id, address, transport=transport, client_id=client_id
        )
        if originator is None:
            # Named for its remedy: when the capability filter is what emptied
            # the candidate list, the fix is upgrading that site server, not
            # declaring one -- and it is recorded instead of, never as well
            # as, `no_site_server`.
            if (
                await _site_server_for(
                    session, org_id, address, transport=None, client_id=client_id
                )
                is None
            ):
                refusals.append("no_site_server")
            elif transport == "winrm":
                refusals.append("no_winrm_site_server")
            else:
                refusals.append("site_server_too_old")
        if device_id is None:
            originator = None
        decisions.append(
            TargetDecision(
                address=str(address),
                client_id=client_id,
                device_id=device_id,
                authorization_id=authorization_id,
                credential_scope_id=scope_id,
                site_server_id=None if originator is None else originator[0],
                site_server_device_id=None if originator is None else originator[1],
                refusals=tuple(refusals),
            )
        )
    return decisions


_CHECKOUT_REQUIRED = N_(
    "a push install requires the `credentials.checkout` permission as well as "
    "`agent.push.run`: a push presents a stored secret to a machine, and "
    "decrypting a stored secret is what `credentials.checkout` governs. "
    "Holding only `agent.push.run` would make this a side door around the "
    "credential module's own gate"
)


@dataclass(frozen=True)
class PushBatchOutcome:
    """What a push request came to. **NEVER A DISPATCH.**

    **THIS IS RETURNED RATHER THAN RAISED, AND THE REASON IS THE TRANSACTION.**
    `org_session` wraps the request in `session.begin()`, which ROLLS BACK on
    any exception leaving the block. A service that wrote `push.refused` and
    then raised inside that block would destroy the very record it had just
    written: the operator would be told "refused" and the audit chain would
    say nothing happened. Invariant #4 requires the opposite -- every
    privileged action is written to the immutable log, and a refused attempt to
    present administrative credentials at somebody's machine is exactly such an
    action.

    So the shape is: the service records, and RETURNS; the route lets the
    transaction commit and only then raises `as_error()`. That is the same
    discipline `ipam/api.py` applies to publishing an envelope after commit,
    pointed in the other direction.

    *(A finding while writing this, not fixed here because it is another
    module's: `ipam`'s `create_sweep` DOES raise inside the block after adding
    its `refused` `ipam_sweep_run` row, so through
    `POST /ipam/subnets/{id}/sweep` that row is rolled back and never reaches
    the database. `tests/test_ipam_sweep_dispatch.py::
    test_the_refused_run_survives_the_raise` does not see it because it commits
    the session itself. Recorded in the cycle report.)*
    """

    decisions: list[TargetDecision]
    refused: dict[str, list[str]]

    @property
    def authorized(self) -> bool:
        """Every named address cleared every bound. Still dispatches nothing."""
        return not self.refused

    def as_error(self) -> PushError | None:
        """The refusal to raise once the transaction has committed, or `None`
        when every bound held and the batch may be dispatched.

        **CYCLE B2 HAS A SUCCESS CASE, WHICH B1 DID NOT**, so this now returns
        `None` for an authorized batch. `dispatch.dispatch_push_batch` calls
        this: on a `PushAuthorizationError` it commits the `push.refused` rows
        and raises 422; on `None` it signs and dispatches. `request_push_batch`
        is once again PURE PREFLIGHT -- it evaluates the ladder and records
        refusals and NOTHING ELSE; the signing lives in `dispatch.py`, which is
        the file the dispatches-nothing guard now exempts for exactly that.
        """
        if self.refused:
            return PushAuthorizationError(
                tr(
                    "this push was refused and nothing was created. Validation is "
                    "all-or-nothing: an operator must not discover mid-batch that some of "
                    "their targets were outside the authorization"
                ),
                self.refused,
            )
        return None


async def request_push_batch(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID,
    credential_id: UUID,
    targets: object,
    now: datetime | None = None,
) -> PushBatchOutcome:
    """Ask for a push. **THE OUTCOME IS ALWAYS A REFUSAL.**

    Raises, in this order and for these reasons -- these write nothing, so
    losing them to the rollback costs no record:

    * `PushPermissionError` -- the caller holds `agent.push.run` and not
      `credentials.checkout` in any client. FIRST, before the batch is even
      parsed, because it is a fact about the CALLER and not about the
      request, and because a caller who may not use a stored secret should
      not be told which of their addresses are authorized.
    * `PushValidationError` -- the batch is empty, over the ceiling, duplicated
      or not a list of host addresses; or the credential is not of type
      `push_install`.
    * `PushNotFoundError` -- the credential is not in this org, or not in a
      client this caller sees. 404, never 403.
    * `PushClientError` -- the credential has no client (a push credential
      has exactly one).
    * `PushPermissionError` -- the caller does not hold `credentials.checkout`
      in THE CREDENTIAL'S CLIENT, the client this push is made in (Task 12:
      a technician holding both codes on one client pushes there).

    Otherwise it records one `push.refused` per address and RETURNS a
    `PushBatchOutcome` whose `as_error()` the caller raises after committing.
    See that class's docstring for why the refusal is not raised from here.

    **`agent.push.run` IS CHECKED AT THE ROUTE AND `credentials.checkout` IS
    CHECKED HERE.** The route dependency can express one code; the conjunction
    is a rule about this CALL, and it lives at the service boundary exactly as
    `provisioning`'s `wake_on_lan` requires `site.wake` there. Without it, a
    push would be a way to USE a stored secret while holding only the
    permission to see its name -- a side door around the credential module's
    own gate (spec §2.5, §8).
    """
    refusal = tr_marked(_CHECKOUT_REQUIRED)
    await require_checkout_somewhere(session, actor_id, refusal)
    addresses = _validated_batch(targets)
    credential = await resolve_push_credential(session, org_id, credential_id)
    client_id = credential_client(credential)
    await require_checkout_in(session, actor_id, client_id, refusal)
    decisions = await evaluate_targets(
        session,
        org_id,
        addresses=addresses,
        credential_id=credential_id,
        client_id=client_id,
        now=now,
    )

    refused = {d.address: list(d.refusals) for d in decisions if d.refusals}
    if refused:
        for address, reasons in sorted(refused.items()):
            await _audit_refusal(
                session,
                org_id,
                actor_id=actor_id,
                address=address,
                credential=credential,
                reasons=reasons,
            )
    # EVERY BOUND HELD -> this function records NOTHING and returns. It is pure
    # preflight; the dispatch that follows (`dispatch.dispatch_push_batch`) is
    # what signs the jobs and audits `push.dispatched` per address, after this
    # ladder has said each target may proceed. Auditing a "would have been
    # allowed" event here as B1 did would now double every real dispatch.
    await session.flush()
    return PushBatchOutcome(decisions=decisions, refused=refused)


async def _audit_refusal(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID,
    address: str,
    credential: object,
    reasons: list[str],
    decision: TargetDecision | None = None,
) -> None:
    """One `push.refused` row per address (spec §9).

    **PER ADDRESS, NOT PER REQUEST.** "Which human caused an administrative
    login to be attempted against THAT machine, with which credential, and why
    it did not happen" is a question about one machine. A single event naming
    twenty-five addresses makes it a question about a request, which is not the
    thing anybody investigates.

    **NEVER THE SECRET, THE `vault_ref`, THE CIPHERTEXT OR ANY DERIVATION OF
    THEM** (invariant #3). The credential appears as its id and the label a
    human gave it, which is what an auditor needs to answer "which credential"
    and is the most that may ever appear in an audit metadata dict.
    """
    metadata: dict[str, object] = {
        "target_address": address,
        "reasons": reasons,
        "credential_id": str(getattr(credential, "id", "")),
        "credential_name": getattr(credential, "name", ""),
    }
    if decision is not None:
        metadata.update(
            {
                "asset_device_id": str(decision.device_id),
                "authorization_id": str(decision.authorization_id),
                "credential_scope_id": str(decision.credential_scope_id),
                "site_server_id": str(decision.site_server_id),
                "site_server_device_id": str(decision.site_server_device_id),
            }
        )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="push.refused",
        target_type="push_target",
        target_id=address,
        # The push's client: the credential's (Task 12).
        client_id=getattr(credential, "client_id", None),
        metadata=metadata,
    )

"""Push-install dispatch: the one file in this module that signs a job and
decrypts a credential (spec §4, §6, §7.1; `CLAUDE.md` invariants #1, #2, #3, 2a).

**THIS IS THE FILE THE DISPATCHES-NOTHING GUARD NOW EXEMPTS, AND THE EXEMPTION
IS NARROW ON PURPOSE.** `tests/test_agent_push_dispatches_nothing.py` was
reshaped, not deleted: every OTHER file in this package still may not name a
signer, a vault or a transport, and NO file in the package -- this one included
-- may open a socket, an SSH session or a WinRM session. The control plane never
connects to a target (invariant #2, 2a clause (a)); that connection is the
AGENT'S, inside a verified job, and nowhere else. What this file is permitted to
do that its siblings are not is exactly what B2 is: create a signed job
(`JobEngine`) and decrypt a stored credential at the point of use (`vault`).

**EVERY PUSH IS AN ORDINARY SIGNED JOB (invariant #1).** `JobEngine.create`
validates the params against the type's model, writes the row, signs over the
canonical bytes with the vault-held key, and `signed_envelope` frames it for the
mTLS WebSocket. The agent's four-check runner verifies it before the capability
runs. There is no second command path and no exemption.

**THE ORIGINATOR IS THE SITE SERVER `preflight._site_server_for` CHOSE, WITH NO
FALLBACK (2a clause (a)).** The job targets the site server's OWN device, never
the machine being installed onto -- which has no `devices` row of the kind a job
can target and must not (this is `site.wake`'s shape exactly). The target
address travels as a SIGNED PARAM the site server may reach and nothing else.

**THE REFUSAL LADDER IS `preflight.request_push_batch`, REUSED, NEVER RECOPIED.**
A second copy of the four bounds is how "the console said it would be refused"
and "the dispatcher signed it anyway" come to both be true. This file adds the
ONE install-only bound the four do not cover -- a confirmed host key (§7.3) --
and it adds it BEFORE any job is created, so validation stays all-or-nothing.

**THE CREDENTIAL LIVES IN THIS PROCESS'S MEMORY FOR ONE REQUEST (spec §6.1).**
It is checked out from the vault immediately before the jobs are created,
carried in the signed params, and never written to a log line, an audit
metadata dict or an API response (the redactor, §4.3). It is scrubbed from
`jobs.params` the instant the job terminates (the scrubber, §6.4). The residual
-- that the plaintext is in a `jobs.params` row for up to the 300s TTL -- is
stated in `jobtypes.py` and in the spec; the durable fix is vault indirection
for job params, a kernel change out of this module's scope.
"""

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import N_, tr, tr_marked
from app.core.storage import ObjectStore
from app.core.vault import VaultService
from app.jobs.engine import DeviceAwaitingReview, JobEngine
from app.models import Client, Device, Job, Site
from app.models.core import AgentRelease
from app.modules.agent_push import credential_realm
from app.modules.agent_push import host_key as host_key_service
from app.modules.agent_push import preflight as preflight_service
from app.modules.agent_push.credential_scope import (
    PUSH_CREDENTIAL_TYPE,
    WINDOWS_DOMAIN_CREDENTIAL_TYPE,
    resolve_push_credential,
)
from app.modules.agent_push.errors import PushTargetClosedError, PushValidationError
from app.modules.agent_push.jobtypes import (
    INSTALL_JOB_TYPE,
    INSTALL_TTL_S,
    PROBE_JOB_TYPE,
    AgentPushInstallParams,
    AgentPushProbeParams,
    PushCredential,
    PushRelease,
)
from app.modules.agent_push.locks import lock_target_scope, sites_of
from app.modules.agent_push.models import PushBatchTarget
from app.modules.agent_push.preflight import (
    PushBatchOutcome,
    TargetDecision,
    transport_for_os_family,
)
from app.modules.agent_push.run_contract import RunTargetDispatch, RunTargetDispatchError
from app.services.client_sites import CLIENT_ARCHIVED, NotAcceptingNewRows, SiteNotFound
from app.services.dns_name import InvalidDnsName, normalise_dns_name
from app.services.enrollment import create_token

__all__ = [
    "PushDispatchResult",
    "DISPATCH_REFUSAL_REASONS",
    "InstallChecks",
    "RunDispatcher",
    "dispatch_push_batch",
    "pre_dispatch_checks",
    "record_probe_result",
    "record_install_result",
]

#: Spec §7.1: tighter than the console-minted token because a machine delivers
#: this one rather than a human typing it. Single-use, and a 30-minute ceiling.
PUSH_ENROLL_TOKEN_MINUTES = 30

#: The install params' `host_key_fingerprint` for a `winrm` push (W4). The field
#: is `min_length=1` and never null on the wire; this sentinel says, in the
#: signed params themselves, that the target is authenticated by Kerberos (a
#: ticket only the real computer account can open, design §2.3) rather than a
#: pinned key. The agent's WinRM path ignores it.
KERBEROS_HOST_KEY_SENTINEL = "kerberos"

#: The one credential type each transport presents (spec §6.2, design §2.5). A
#: `push_install` (SSH) secret is never shown to a WinRM target and a Windows
#: domain password never to an SSH one: the dedication rule, per transport.
CREDENTIAL_TYPE_FOR_TRANSPORT = {
    "ssh": PUSH_CREDENTIAL_TYPE,
    "winrm": WINDOWS_DOMAIN_CREDENTIAL_TYPE,
}


#: The one sentence an operator sees when the console's address is not
#: configured and a push would therefore hand the target nowhere to enroll.
CONSOLE_URL_UNSET = N_("Set ITS_PUBLIC_CONSOLE_URL so a pushed computer knows where to enroll.")

#: The authority an enroll origin may have: a DNS name or IPv4 literal and an
#: optional port -- exactly what the agent's installers accept
#: (`layout.CheckEnrollOrigin`). Nothing a shell, PowerShell or XML would read.
_ORIGIN_AUTHORITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}(?::([0-9]{1,5}))?")


#: The refusals dispatch adds to the ladder's (preflight.REFUSAL_REASONS), each
#: with the remedy it implies (review Minor 2). Plain language: no type code,
#: no field name, no device id -- the console shows these beside the
#: machine's own address.
DISPATCH_REFUSAL_REASONS: dict[str, str] = {
    "no_dns_name": (
        N_(
            "a Windows machine is pushed with Kerberos, which needs the computer's full "
            "DNS name, not its address. Set its DNS name on the machine's page (Push "
            "install details)"
        )
    ),
    "invalid_dns_name": (
        N_(
            "the machine's DNS name is not a usable full DNS name: letters, digits and "
            "hyphens, with its domain, and not an address. Correct it on the machine's page"
        )
    ),
    "wrong_credential_kind": (
        N_(
            "the chosen credential is the wrong kind for this machine: Windows machines "
            "take a Windows domain account, Linux and macOS machines a push install "
            "account. Choose a credential of the machine's kind"
        )
    ),
    "no_console_url": N_(
        "this server's own address is not configured as a plain https address, so a "
        "pushed machine would not know where to enroll. Set ITS_PUBLIC_CONSOLE_URL so a "
        "pushed computer knows where to enroll."
    ),
    "outside_realm": (
        N_(
            "the machine's DNS name is not inside the Active Directory domain the chosen "
            "Windows account signs in to, and an account is presented only to machines in "
            "its own domain. Check the machine's DNS name, or choose an account from its "
            "domain"
        )
    ),
    "realm_not_recorded": (
        N_(
            "the chosen Windows account was saved before its domain was recorded. Open it "
            "under Credentials and save it again"
        )
    ),
    "realm_changed": (
        N_(
            "the chosen Windows account now names a different domain from the one recorded "
            "for it. Open it under Credentials and save it again"
        )
    ),
    # MSP clients Task 9: an archived site or client enrols nothing, so a
    # pushed agent would install and never appear.
    "site_archived": (
        N_(
            "the machine is filed under an archived site, or under a client that has been "
            "archived, and nothing is enrolled into either. Push only machines of active sites "
            "and clients"
        )
    ),
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _record_refusals(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    credential: Any,
    refusals: dict[str, list[str]],
    extras: dict[str, dict[str, str]],
) -> None:
    """`push.refused`, one row per refused address, exactly as the ladder
    writes them, in the push's client (its credential's); the route commits
    them and then raises 422."""
    for address, reasons in sorted(refusals.items()):
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="push.refused",
            target_type="push_target",
            target_id=address,
            client_id=credential.client_id,
            metadata={
                "target_address": address,
                "reasons": reasons,
                "credential_id": str(credential.id),
                "credential_name": credential.name,
                **extras.get(address, {}),
            },
        )
    await session.flush()


def _enroll_base_url(public_console_url: str | None) -> str:
    """Where a pushed computer enrolls: `settings.public_console_url`
    (ITS_PUBLIC_CONSOLE_URL) as a bare https origin, or a refusal.

    **PLAN W2, SUPERSEDED 2026-09-29.** W2 had the agent derive the enroll URL
    from the presigned `release.url`'s origin. That origin is the console only
    with the bundled MinIO; with external object storage (real S3, Wasabi) it
    is the storage provider, which would then be sent the single-use
    enrollment token. The URL is server CONFIGURATION, carried in the signed
    params -- never operator-authored and never inferred from another value.

    The value is what the installed service's `ITS_AGENT_ENROLL_URL` holds: the
    API base with no path. The agent's enrollment client appends `/enroll/v1`
    itself (`agent/internal/enroll` `EnrollPath`), and the agent's install
    script accepts only a bare https origin, so anything else -- a path, a
    query, userinfo, plain http -- is refused here, before the credential is
    decrypted, rather than failing on the target after it was presented.
    """
    raw = (public_console_url or "").strip().rstrip("/")
    if not raw:
        raise PushValidationError(tr_marked(CONSOLE_URL_UNSET))
    parts = urlsplit(raw)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or not _origin_authority_ok(parts.netloc)
    ):
        raise PushValidationError(
            tr(
                "ITS_PUBLIC_CONSOLE_URL={raw} is not the console's bare https address (for "
                "example https://itms.example.com, with no path). {console_url_unset}",
                raw=repr(raw),
                console_url_unset=tr_marked(CONSOLE_URL_UNSET),
            )
        )
    return f"https://{parts.netloc}"


def _origin_authority_ok(netloc: str) -> bool:
    match = _ORIGIN_AUTHORITY.fullmatch(netloc)
    if match is None:
        return False
    port = match.group(1)
    return port is None or 1 <= int(port) <= 65535


@dataclass(frozen=True)
class _DispatchedTarget:
    address: str
    job_id: UUID
    device_id: UUID | None
    site_server_device_id: UUID | None
    envelope: dict[str, Any]


@dataclass(frozen=True)
class PushDispatchResult:
    """What a push request came to. Refused, or dispatched -- never both.

    `envelopes` are published by the CALLER after the tenant transaction
    commits, exactly as `site.wake` does: a job handed to an agent describing
    state that rolled back would be a push nobody authorized, and this one
    presents an administrative credential.
    """

    batch_id: UUID | None
    kind: str
    refused: dict[str, list[str]] = field(default_factory=dict)
    dispatched: list[_DispatchedTarget] = field(default_factory=list)

    @property
    def envelopes(self) -> list[dict[str, Any]]:
        """The signed envelopes to publish after commit. The publish target is
        `envelope["job"]["device"]` -- the site server's device -- exactly as
        the `site.wake` route reads it."""
        return [t.envelope for t in self.dispatched]


def _parse_credential_secret(
    raw: str, *, credential_type: str = PUSH_CREDENTIAL_TYPE
) -> tuple[str, str]:
    """A push credential's vault plaintext is a JSON object
    `{"username": ..., "secret": ...}`; return `(username, wire_secret)` or raise.

    **`wire_secret` IS WHAT GOES INTO `PushCredential.secret`, AND ITS SHAPE
    DEPENDS ON THE TYPE.** For `push_install` (SSH) it is the bare password, as
    it always was. For `windows_domain` (WinRM, design §2.5) the vault blob also
    carries an optional explicit KDC and an optional AD CS root PEM for 5986
    trust (§I3/W9); those travel INSIDE the secret as the JSON
    `{"secret": <password>, "kdc": <host[:port] or "">, "ca_bundle": <PEM or "">}`
    so the wire model stays `{username, secret}` -- no new field, no wire
    change -- and the redactor and scrubber, which strip `credential` wholesale,
    cover the KDC and CA with no new code. The username is carried in
    `PushCredential.username` and is not repeated in the blob. The agent's
    winrm path re-parses `secret` as this JSON; its ssh path uses it verbatim.

    **WHY A JSON DOCUMENT RATHER THAN TWO COLUMNS.** The `credentials` table
    stores one opaque secret string and no username column, and adding one is a
    kernel-model change that is not this module's to make. So the username
    travels INSIDE the encrypted blob -- decrypted only here, at the point of
    use, never in a column, an API response or a log (invariant #3). A stored
    secret that is not this shape is a configuration error named plainly, not a
    best-effort guess at which half is the username.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PushValidationError(
            tr(
                "a {credential_type} credential's secret must be a JSON object "
                '{{"username": "...", "secret": "..."}}; the stored value is not JSON',
                credential_type=credential_type,
            )
        ) from exc
    if not isinstance(parsed, dict):
        raise PushValidationError(
            tr(
                "a {credential_type} credential's secret must be a JSON object with "
                '"username" and "secret"',
                credential_type=credential_type,
            )
        )
    username = parsed.get("username")
    secret = parsed.get("secret")
    if not isinstance(username, str) or not username:
        raise PushValidationError(
            tr(
                'a {credential_type} credential\'s secret is missing a non-empty "username"',
                credential_type=credential_type,
            )
        )
    if not isinstance(secret, str) or not secret:
        raise PushValidationError(
            tr(
                'a {credential_type} credential\'s secret is missing a non-empty "secret"',
                credential_type=credential_type,
            )
        )
    if credential_type != WINDOWS_DOMAIN_CREDENTIAL_TYPE:
        return username, secret
    optional: dict[str, str] = {}
    for key in ("kdc", "ca_bundle"):
        value = parsed.get(key)
        if value is None:
            optional[key] = ""
        elif isinstance(value, str):
            optional[key] = value
        else:
            raise PushValidationError(
                tr(
                    "a windows_domain credential's {key} must be a string when present",
                    key=repr(key),
                )
            )
    return username, json.dumps({"secret": secret, **optional})


async def _resolve_release(
    session: AsyncSession, os_name: str, arch: str
) -> AgentRelease:
    """The newest published, digest-bearing artefact for this OS/arch, or raise.

    **SERVER-SELECTED, NEVER OPERATOR-SUPPLIED (spec §4.2).** The operator names
    no version, no URL and no artefact: they declare the OS/arch of the machine
    they promoted, and the server picks the release. An empty catalogue is a
    refusal named plainly (spec §1.3 item 4: a push with nothing to push is not
    a feature), never a dispatch against a dangling reference.

    `agent_releases` is GLOBAL (no `org_id`, spec §4.2), so this query is NOT
    tenant-filtered and must not be assumed to be. That is fine: the artefact
    bytes are identical for every tenant; what is tenant-scoped is the RIGHT to
    push, enforced by the ladder before this is ever called.
    """
    row = (
        await session.execute(
            select(AgentRelease)
            .where(
                AgentRelease.os == os_name,
                AgentRelease.arch == arch,
                AgentRelease.sha256 != "",
                AgentRelease.published_at.is_not(None),
            )
            .order_by(AgentRelease.published_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise PushValidationError(
            tr(
                "no published agent release exists for {os_name}/{arch}. A push has "
                "nothing to install until an artefact is published for the target's OS "
                "(spec §1.3 item 4). Publish one, or install the agent by hand",
                os_name=os_name,
                arch=arch,
            )
        )
    return row


@dataclass(frozen=True)
class InstallChecks:
    """Every bound the ladder does not cover, found for every target BEFORE any
    job is created and BEFORE any credential is decrypted (review Minor 2).

    Extracted from `dispatch_push_batch` so that a ticked push, a run's
    expansion (`run_list.expand_prefix`) and the pacer (`RunDispatcher`) ask
    the SAME function. A second copy is how "the console said ready" and "the
    dispatcher refused" come to disagree.
    """

    refusals: dict[str, list[str]]
    extras: dict[str, dict[str, str]]
    transports: dict[str, tuple[str, str | None]]
    platforms: dict[str, tuple[str, str]]
    confirmed: dict[str, str]
    enroll_url: str
    realm: str | None


async def pre_dispatch_checks(
    session: AsyncSession,
    org_id: UUID,
    *,
    decisions: Sequence[TargetDecision],
    credential: Any,
    kind: str,
    public_console_url: str | None,
) -> InstallChecks:
    """Reads only. Raises `PushValidationError` where `_device_target` does
    (an install onto an undeclared OS or arch), exactly as before extraction."""
    refusals: dict[str, list[str]] = {}
    extras: dict[str, dict[str, str]] = {}

    def refuse(address: str, reason: str, **detail: str) -> None:
        refusals.setdefault(address, []).append(reason)
        extras.setdefault(address, {}).update(detail)

    # THE TRANSPORT IS THE TARGET'S OS'S (design §2.1). A Windows target with no
    # DNS name, or one that is not a usable DNS name, cannot be authenticated
    # with Kerberos (`HTTP/<fqdn>`, design §2.3, W10). An install also
    # validates the declared OS and arch (`_device_target`); a probe needs
    # only the transport (`_device_transport`).
    transports: dict[str, tuple[str, str | None]] = {}
    # THE TARGET IS READ ONCE (Codex on #90). The install job is built from
    # this same snapshot, never a second read: under READ COMMITTED an edit
    # landing between two reads would pair the new OS's transport and release
    # with the credential and host key validated for the old one.
    platforms: dict[str, tuple[str, str]] = {}
    for decision in decisions:
        assert decision.device_id is not None
        if kind == "install":
            os_name, arch, transport, raw_name = await _device_target(
                session, org_id, decision.device_id
            )
            platforms[decision.address] = (os_name, arch)
        else:
            transport, raw_name = await _device_transport(session, org_id, decision.device_id)
        dns_name: str | None = None
        if transport == "winrm":
            dns_name, reason = _dns_name_refusal(raw_name)
            if reason is not None:
                refuse(decision.address, reason, dns_name=raw_name or "")
        transports[decision.address] = (transport, dns_name)

    enroll_url = ""
    realm: str | None = None
    confirmed: dict[str, str] = {}
    if kind == "install":
        # THE CREDENTIAL MUST BE THE TRANSPORT'S (spec §6.2 per transport): an
        # SSH secret is never presented to a WinRM target, nor a Windows domain
        # password to an SSH one.
        for address, (transport, _name) in sorted(transports.items()):
            if credential.type != CREDENTIAL_TYPE_FOR_TRANSPORT[transport]:
                refuse(address, "wrong_credential_kind")

        # WHERE A PUSHED COMPUTER ENROLLS (W2 superseded): the configured
        # console, for every transport. Without it the pushed agent has
        # nowhere to enroll, so every target of the batch is refused.
        try:
            enroll_url = _enroll_base_url(public_console_url)
        except PushValidationError:
            for address in transports:
                refuse(address, "no_console_url")

        # THE REALM IS THE CREDENTIAL'S (review I1), recorded when it was
        # saved, and every Windows target must be inside it. The KDC that will
        # see the account's pre-authentication is found for THIS realm, never
        # for whatever DNS name a device row carries.
        winrm_targets = {
            address: name for address, (transport, name) in transports.items()
            if transport == "winrm" and name
        }
        if winrm_targets and credential.type == CREDENTIAL_TYPE_FOR_TRANSPORT["winrm"]:
            realm = await credential_realm.realm_for(session, org_id, credential.id)
            for address, name in sorted(winrm_targets.items()):
                if realm is None:
                    refuse(address, "realm_not_recorded", dns_name=name)
                elif not credential_realm.fqdn_in_realm(name, realm):
                    refuse(address, "outside_realm", dns_name=name, realm=realm)

        # AN ARCHIVED SITE OR CLIENT ENROLS NOTHING (MSP clients Task 9): the
        # token minted for the target's site would be refused by `create_token`
        # and every enrollment refused by `enroll`. Named here, before the
        # decrypt, rather than discovered at the mint.
        for decision in decisions:
            assert decision.device_id is not None
            if await _site_closed(session, org_id, decision.device_id):
                refuse(decision.address, "site_archived")

        # THE ONE INSTALL-ONLY BOUND THE FOUR DO NOT COVER: a confirmed host key
        # (§7.3), for every SSH target. A push to a host with no confirmed key,
        # or one whose key has changed, is refused -- not prompted, not warned
        # about. A WINRM target has no host key to pin: Kerberos authenticates
        # it (design §2.3, W4), so it carries the `kerberos` sentinel instead.
        for decision in decisions:
            if transports[decision.address][0] == "winrm":
                confirmed[decision.address] = KERBEROS_HOST_KEY_SENTINEL
                continue
            # The target device's client's key (migration 0231, Task 8b): two
            # clients' machines at one address are two hosts.
            fingerprint, reason = await host_key_service.confirmed_key_for(
                session, org_id, decision.address, device_id=decision.device_id
            )
            if fingerprint is None:
                assert reason is not None
                refuse(decision.address, reason)
            else:
                confirmed[decision.address] = fingerprint

    return InstallChecks(
        refusals=refusals,
        extras=extras,
        transports=transports,
        platforms=platforms,
        confirmed=confirmed,
        enroll_url=enroll_url,
        realm=realm,
    )


async def dispatch_push_batch(
    session: AsyncSession,
    job_engine: JobEngine,
    object_store_factory: Callable[[], ObjectStore] | None,
    vault: VaultService,
    *,
    org_id: UUID,
    actor_id: UUID,
    credential_id: UUID,
    targets: object,
    kind: str,
    now: datetime | None = None,
    public_console_url: str | None = None,
) -> PushDispatchResult:
    """Run the ladder and, if every bound holds, sign and create one job per
    target. `kind` is `"probe"` or `"install"`.

    `public_console_url` is `settings.public_console_url`: an install, over
    either transport, is refused before the credential is decrypted when it is
    unset (see `_enroll_base_url`, plan W2 superseded).

    Returns a `PushDispatchResult`. The caller (the API route) publishes the
    envelopes after the transaction commits, and on a refusal raises 422 after
    the `push.refused` rows commit.
    """
    if kind not in ("probe", "install"):
        raise PushValidationError(tr("unknown push kind {kind}", kind=repr(kind)))
    stamp = now or _utcnow()

    # THE FOUR BOUNDS, RUN ONCE, IN THE ONE FUNCTION THAT DECIDES THEM. This
    # also does the `credentials.checkout` conjunction, the batch validation and
    # the credential-type check, and audits `push.refused` per address for any
    # target that failed. Reused, never recopied (see the module docstring).
    outcome: PushBatchOutcome = await preflight_service.request_push_batch(
        session,
        org_id,
        actor_id=actor_id,
        credential_id=credential_id,
        targets=targets,
        now=stamp,
    )
    if outcome.refused:
        # The `push.refused` rows are already written; the route commits them
        # and raises 422. Nothing is signed.
        return PushDispatchResult(batch_id=None, kind=kind, refused=outcome.refused)

    credential = await resolve_push_credential(session, org_id, credential_id)

    # EVERY BOUND THE LADDER DOES NOT COVER, FOR EVERY TARGET, BEFORE ANY JOB IS
    # CREATED AND BEFORE ANY CREDENTIAL IS DECRYPTED -- `pre_dispatch_checks`,
    # the one function the run's expansion and the pacer also call.
    checks = await pre_dispatch_checks(
        session,
        org_id,
        decisions=outcome.decisions,
        credential=credential,
        kind=kind,
        public_console_url=public_console_url,
    )
    if checks.refusals:
        await _record_refusals(
            session, org_id=org_id, actor_id=actor_id, credential=credential,
            refusals=checks.refusals, extras=checks.extras,
        )
        return PushDispatchResult(batch_id=None, kind=kind, refused=checks.refusals)
    transports = checks.transports
    platforms = checks.platforms
    confirmed = checks.confirmed
    enroll_url = checks.enroll_url
    realm = checks.realm

    if kind == "install":
        # LOCK ORDER (research-4 §I5): every target's site and client FOR SHARE
        # NOW, before the first target's `push.dispatched` takes the audit
        # chain and the next target's token mint would take them after it.
        # Probes mint no token.
        await lock_target_scope(
            session,
            org_id,
            await sites_of(
                session, org_id, (d.device_id for d in outcome.decisions if d.device_id)
            ),
        )

    batch_id = uuid4()
    dispatched: list[_DispatchedTarget] = []

    if kind == "probe":
        for decision in outcome.decisions:
            transport, dns_name = transports[decision.address]
            job, envelope = await _dispatch_probe(
                session,
                job_engine,
                org_id=org_id,
                actor_id=actor_id,
                decision=decision,
                transport=transport,
                dns_name=dns_name,
                credential_name=credential.name,
                credential_id=credential.id,
                batch_id=batch_id,
            )
            dispatched.append(
                _DispatchedTarget(
                    address=decision.address,
                    job_id=job.id,
                    device_id=decision.device_id,
                    site_server_device_id=decision.site_server_device_id,
                    envelope=envelope,
                )
            )
        await session.flush()
        return PushDispatchResult(batch_id=batch_id, kind=kind, dispatched=dispatched)

    # INSTALL. Decrypt the credential ONCE, at the point of use (invariant #3),
    # and reuse it across the batch. `vault.checkout` returns the plaintext
    # JSON blob; it is parsed here and never leaves this function except inside
    # the signed params, which are redacted on read and scrubbed on terminal.
    # THE OBJECT STORE IS BUILT ONLY NOW -- after the ladder, the credential
    # check and the host-key check have all passed. Building it eagerly in the
    # route would touch S3 before the `credentials.checkout` refusal, turning a
    # clean 403 into a 500 for a caller who may not use a stored secret at all.
    if object_store_factory is None:
        raise PushValidationError(
            tr(
                "an install requires an object store to presign the agent artefact; none "
                "was supplied to dispatch_push_batch"
            )
        )
    object_store = object_store_factory()
    raw = await vault.checkout(session, org_id=org_id, credential_id=credential_id)
    username, secret = _parse_credential_secret(raw, credential_type=credential.type)
    if realm is not None:
        # Belt and braces: the account presented is in the realm the targets
        # were just checked against (a rotation cannot have moved it without
        # re-recording the realm, but this is the line that makes it so). Found
        # only after the decrypt, because the account is inside the secret;
        # still recorded as a refusal, and nothing is signed.
        try:
            account_realm: str | None = credential_realm.realm_of_account(username)
        except credential_realm.InvalidWindowsDomainAccount:
            account_realm = None
        if account_realm != realm:
            changed = {address: ["realm_changed"] for address in sorted(transports)}
            await _record_refusals(
                session, org_id=org_id, actor_id=actor_id, credential=credential,
                refusals=changed,
                extras={a: {"realm": realm} for a in changed},
            )
            return PushDispatchResult(batch_id=None, kind=kind, refused=changed)

    for decision in outcome.decisions:
        job, envelope = await _dispatch_install(
            session,
            job_engine,
            object_store,
            org_id=org_id,
            actor_id=actor_id,
            decision=decision,
            username=username,
            secret=secret,
            credential_name=credential.name,
            credential_id=credential.id,
            host_key_fingerprint=confirmed[decision.address],
            os_name=platforms[decision.address][0],
            arch=platforms[decision.address][1],
            transport=transports[decision.address][0],
            dns_name=transports[decision.address][1],
            enroll_url=enroll_url,
            batch_id=batch_id,
        )
        dispatched.append(
            _DispatchedTarget(
                address=decision.address,
                job_id=job.id,
                device_id=decision.device_id,
                site_server_device_id=decision.site_server_device_id,
                envelope=envelope,
            )
        )
    await session.flush()
    return PushDispatchResult(batch_id=batch_id, kind=kind, dispatched=dispatched)


async def _dispatch_probe(
    session: AsyncSession,
    job_engine: JobEngine,
    *,
    org_id: UUID,
    actor_id: UUID,
    decision: TargetDecision,
    transport: str,
    dns_name: str | None,
    credential_name: str,
    credential_id: UUID,
    batch_id: UUID,
) -> tuple[Job, dict[str, Any]]:
    """One `agent.push_probe` job. **CARRIES NO CREDENTIAL** (spec §4.4).

    Over `winrm` it carries the target's DNS name too, and still nothing that
    could hold a secret: the WinRM probe checks 5986/5985 and KDC reachability
    with no AS exchange (design §2.4, §C1).
    """
    assert decision.site_server_device_id is not None
    params = AgentPushProbeParams(
        target_address=decision.address,
        transport="winrm" if transport == "winrm" else "ssh",
        dns_name=dns_name,
    )
    job = await job_engine.create(
        session,
        org_id=org_id,
        target_device_id=decision.site_server_device_id,
        type=PROBE_JOB_TYPE,
        params=params.model_dump(),
        requested_by=actor_id,
    )
    await _record_batch_target(
        session,
        org_id=org_id,
        batch_id=batch_id,
        kind="probe",
        decision=decision,
        job_id=job.id,
        credential_id=credential_id,
        actor_id=actor_id,
    )
    await _audit_dispatched(
        session,
        org_id=org_id,
        actor_id=actor_id,
        kind="probe",
        decision=decision,
        job=job,
        credential_id=credential_id,
        credential_name=credential_name,
        release=None,
        enroll_token_id=None,
    )
    envelope = await job_engine.signed_envelope(job)
    return job, envelope


async def _dispatch_install(
    session: AsyncSession,
    job_engine: JobEngine,
    object_store: ObjectStore,
    *,
    org_id: UUID,
    actor_id: UUID,
    decision: TargetDecision,
    username: str,
    secret: str,
    credential_name: str,
    credential_id: UUID,
    host_key_fingerprint: str,
    os_name: str,
    arch: str,
    transport: str,
    dns_name: str | None,
    enroll_url: str,
    batch_id: UUID,
    run_id: UUID | None = None,
) -> tuple[Job, dict[str, Any]]:
    """One `agent.push_install` job: mint the token, presign the artefact,
    build the whole signed instruction, and audit it -- from the target
    snapshot `dispatch_push_batch` validated, never a fresh read."""
    assert decision.device_id is not None
    assert decision.site_server_device_id is not None

    release = await _resolve_release(session, os_name, arch)

    # A single-use, <=30-minute token (spec §7.1). It DELIVERS no identity: the
    # installed agent enrols itself with its own keypair, and the token's name
    # ties the eventual enrolment back to this push for `push.enrolled`.
    #
    # THE TARGET ROW'S SITE (MSP clients spec §1.5). The agent enrols under
    # the token's client alone, so the asset row it is meant to join has to
    # be of that client -- it is, because the token is minted for its site.
    target_site = (
        await session.execute(
            select(Device.site_id).where(
                Device.id == decision.device_id, Device.org_id == org_id
            )
        )
    ).scalar_one()
    try:
        issued = await create_token(
            session,
            org_id=org_id,
            site_id=target_site,
            created_by=actor_id,
            name=f"push:{batch_id}:{decision.address}",
            expires_in_hours=None,
            expires_in_minutes=PUSH_ENROLL_TOKEN_MINUTES,
            max_uses=1,
        )
    except (SiteNotFound, NotAcceptingNewRows):
        # `pre_dispatch_checks` refused an archived site by name; reaching this
        # means it was archived since (or is not this session's to see). A
        # refusal, never a 500: the ticked push's route rolls the batch back
        # and answers 409, the pacer records `site_archived` for this target.
        raise PushTargetClosedError(
            tr(
                "the machine at {address} is filed under a site or client that is archived "
                "or not visible to you; nothing was dispatched",
                address=decision.address,
            )
        ) from None

    url = object_store.presign_agent_release_get(
        release.version, os_name, arch, expires_in=INSTALL_TTL_S
    )

    params = AgentPushInstallParams(
        target_address=decision.address,
        expected_mac=None,
        transport="winrm" if transport == "winrm" else "ssh",
        os=_WIRE_OS[os_name],
        arch="arm64" if arch == "arm64" else "amd64",
        dns_name=dns_name,
        release=PushRelease(version=release.version, sha256=release.sha256, url=url),
        enroll_token=issued.token,
        credential=PushCredential(username=username, secret=secret),
        # The confirmed SSH key, or the `kerberos` sentinel for winrm (W4).
        host_key_fingerprint=host_key_fingerprint,
        # The configured console (W2 superseded); never the presigned URL's
        # origin, which on external object storage is the storage provider.
        enroll_url=enroll_url,
    )
    job = await job_engine.create(
        session,
        org_id=org_id,
        target_device_id=decision.site_server_device_id,
        type=INSTALL_JOB_TYPE,
        params=params.model_dump(),
        requested_by=actor_id,
        ttl_s=INSTALL_TTL_S,
    )
    await _record_batch_target(
        session,
        org_id=org_id,
        batch_id=batch_id,
        kind="install",
        decision=decision,
        job_id=job.id,
        credential_id=credential_id,
        actor_id=actor_id,
    )
    await _audit_dispatched(
        session,
        org_id=org_id,
        actor_id=actor_id,
        kind="install",
        decision=decision,
        job=job,
        credential_id=credential_id,
        credential_name=credential_name,
        release=release,
        enroll_token_id=issued.id,
        run_id=run_id,
    )
    envelope = await job_engine.signed_envelope(job)
    return job, envelope


_OS_MAP = {"linux": "linux", "macos": "darwin", "darwin": "darwin", "windows": "windows"}
_WIRE_OS: dict[str, Literal["linux", "darwin", "windows"]] = {
    "linux": "linux",
    "darwin": "darwin",
    "windows": "windows",
}


def _dns_name_refusal(fqdn: str | None) -> tuple[str | None, str | None]:
    """`(dns_name, None)` for a usable DNS name, or `(None, reason)`.

    The DNS name a WinRM push authenticates to (design §2.3, W10). It must be a
    real DNS host name -- `normalise_dns_name`, the SAME check the asset form
    applies (review I1): inventory writes this column on a managed device, so
    what reaches here has not necessarily passed that form. Not an IP literal,
    a dot required, labels of letters, digits and hyphens. **NO REVERSE-DNS
    CANONICALISATION**: resolving the IP back to a name would let DNS decide
    which SPN a credential is presented for. Returned lower-cased.
    """
    name = (fqdn or "").strip()
    if not name:
        return None, "no_dns_name"
    try:
        return normalise_dns_name(name), None
    except InvalidDnsName:
        return None, "invalid_dns_name"


async def _device_row(
    session: AsyncSession, org_id: UUID, device_id: UUID
) -> tuple[str, str, str | None]:
    """`(os_family, architecture, fqdn)` for the promoted asset, blanks if unset."""
    from sqlalchemy import text

    row = (
        await session.execute(
            text(
                "SELECT os_family, architecture, fqdn FROM devices"
                " WHERE org_id = :org AND id = :id"
            ),
            {"org": str(org_id), "id": str(device_id)},
        )
    ).first()
    if row is None:
        return "", "", None
    return row.os_family or "", row.architecture or "", row.fqdn or None


async def _device_transport(
    session: AsyncSession, org_id: UUID, device_id: UUID
) -> tuple[str, str | None]:
    """`(transport, dns_name)` for a target -- what a PROBE needs.

    Deliberately lighter than `_device_target`: a probe fetches readiness or a
    host key and installs nothing, so it does not require a declared arch, and
    an SSH probe of an asset whose OS is not yet declared keeps working exactly
    as it did. What it DOES require is what the transport cannot work without:
    a Windows target's hostname.
    """
    os_family, _arch, fqdn = await _device_row(session, org_id, device_id)
    transport = transport_for_os_family(os_family)
    return transport, (fqdn if transport == "winrm" else None)


async def _site_closed(session: AsyncSession, org_id: UUID, device_id: UUID) -> bool:
    """Whether the target device's site is archived, or its client is."""
    found = (
        await session.execute(
            select(Site.archived_at, Client.status)
            .join(Device, Device.site_id == Site.id)
            .join(Client, Client.id == Site.client_id)
            .where(Device.id == device_id, Device.org_id == org_id)
        )
    ).one_or_none()
    if found is None:
        return False  # the ladder's own bounds answer a device it cannot see
    archived_at, client_status = found
    return archived_at is not None or client_status == CLIENT_ARCHIVED


async def _device_target(
    session: AsyncSession, org_id: UUID, device_id: UUID
) -> tuple[str, str, str, str | None]:
    """The promoted asset's OS, arch, TRANSPORT and DNS name (design §2.1).

    The operator declared OS/arch at promotion; the pushing agent verifies them
    on the target after authenticating (§7.2), so a wrong declaration is a
    clean failure, never a best-effort install of the wrong binary. The
    transport follows the OS: Windows over WinRM (Kerberos only), Linux and
    macOS over SSH. A WINDOWS TARGET WITH NO DNS NAME -- OR ONE THAT IS NOT A
    HOSTNAME -- IS REFUSED HERE, and `dispatch_push_batch` reaches this (via
    `_device_transport`) before any credential is decrypted (design §2.3, W10).
    """
    os_family, arch, fqdn = await _device_row(session, org_id, device_id)
    mapped = _OS_MAP.get(os_family.lower())
    if mapped is None:
        raise PushValidationError(
            tr(
                "the promoted asset at {device_id} declares os_family {os_family}, which "
                "push cannot install onto. Supported: linux, macOS (SSH) and Windows "
                "(WinRM). Set the asset's OS",
                device_id=device_id,
                os_family=repr(os_family),
            )
        )
    if arch.lower() not in ("amd64", "arm64"):
        raise PushValidationError(
            tr(
                "the promoted asset at {device_id} declares arch {arch}; push needs amd64 "
                "or arm64 to select the right artefact (§7.2)",
                device_id=device_id,
                arch=repr(arch),
            )
        )
    transport = transport_for_os_family(mapped)
    return mapped, arch.lower(), transport, (fqdn if transport == "winrm" else None)


async def _record_batch_target(
    session: AsyncSession,
    *,
    org_id: UUID,
    batch_id: UUID,
    kind: str,
    decision: TargetDecision,
    job_id: UUID,
    credential_id: UUID,
    actor_id: UUID,
) -> None:
    session.add(
        PushBatchTarget(
            org_id=org_id,
            batch_id=batch_id,
            kind=kind,
            address=decision.address,
            job_id=job_id,
            device_id=decision.device_id,
            site_server_device_id=decision.site_server_device_id,
            credential_id=credential_id,
            created_by=actor_id,
        )
    )
    await session.flush()


async def _audit_dispatched(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    kind: str,
    decision: TargetDecision,
    job: Job,
    credential_id: UUID,
    credential_name: str,
    release: AgentRelease | None,
    enroll_token_id: UUID | None,
    run_id: UUID | None = None,
) -> None:
    """`push.dispatched` (spec §9). **NEVER THE SECRET, THE TOKEN PLAINTEXT OR
    THE PRESIGNED URL** -- only the ids and names an auditor needs to answer
    "which human caused an administrative login to that machine, with which
    credential, from which host, and what got installed on it".
    """
    metadata: dict[str, Any] = {
        "kind": kind,
        "target_address": decision.address,
        "promoted_asset_id": None if decision.device_id is None else str(decision.device_id),
        "credential_id": str(credential_id),
        "credential_name": credential_name,
        "site_server_device_id": (
            None
            if decision.site_server_device_id is None
            else str(decision.site_server_device_id)
        ),
        "job_id": str(job.id),
        "authorization_id": (
            None if decision.authorization_id is None else str(decision.authorization_id)
        ),
    }
    if release is not None:
        metadata["release_version"] = release.version
        metadata["release_sha256"] = release.sha256
    if enroll_token_id is not None:
        metadata["enrollment_token_id"] = str(enroll_token_id)
    if run_id is not None:
        # THE PACER ACTED, ON BEHALF OF THE PERSON WHO CONFIRMED THE RUN
        # (CLAUDE.md 2a(e) as amended 2026-09-30; agent_update's precedent).
        # The chain must not say a human clicked at this moment; it names the
        # human whose confirmation this continues, and the run.
        metadata["run_id"] = str(run_id)
        metadata["on_behalf_of"] = str(actor_id)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system" if run_id is not None else "user",
        actor_id=None if run_id is not None else actor_id,
        action="push.dispatched",
        target_type="push_target",
        target_id=decision.address,
        # The push's client -- its credential's -- in which the ladder found
        # this machine and its site server (Task 12).
        client_id=decision.client_id,
        metadata=metadata,
    )


#: Domain separation for `credential_fingerprint`, so the digest can never be
#: mistaken for, or collide with, any other SHA-256 this platform computes.
_CREDENTIAL_FINGERPRINT_DOMAIN = b"it-suite/push-run/credential-version/v1\x00"


def credential_fingerprint(credential: Any) -> str:
    """AN OPAQUE NAME FOR WHICH STORED VERSION OF A CREDENTIAL'S SECRET THIS IS
    (spec §1.4a; review item 2). The lockout guard counts a proven sign-in only
    for the version that was proven.

    SHA-256, domain-separated and length-prefixed, over the vault ENVELOPE as
    stored -- `ciphertext` and `wrapped_dek` -- never over the plaintext. It
    therefore cannot be turned into the secret: a preimage would yield only
    ciphertext the database already holds, and that is AES-256-GCM under a DEK
    the database cannot unwrap. Every store re-encrypts under a fresh nonce,
    so any save of a secret, even of the same password again, is a new version:
    the conservative direction, one first again.

    It is kept on `push_run_target` for comparison only. No API response,
    audit row or log carries it."""
    digest = hashlib.sha256(_CREDENTIAL_FINGERPRINT_DOMAIN)
    for part in (credential.ciphertext, credential.wrapped_dek):
        data = (part or "").encode()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


class RunDispatcher:
    """The pacer's ONE way to dispatch one confirmed run target
    (design §1.4; CLAUDE.md 2a(e) as amended 2026-09-30).

    **THE SAME PATH A TICKED PUSH TAKES.** `pre_dispatch_checks` for the
    install-only bounds, `_resolve_release`, then `vault.checkout` at this
    moment, for this one target, then `_dispatch_install`, which mints the
    single-use 30-minute token, presigns, signs the job for the segment's site
    server and audits `push.dispatched` with the run's id.

    **A REFUSAL IS RETURNED, NEVER RAISED, AND NEVER RECORDED HERE.** The
    pacer writes `push.refused` with the run's context. Only failures that
    would recur on every tick are named refusals (R12): anything else
    propagates, so a signer outage refuses no target. Once the secret has been
    checked out it propagates as `RunTargetDispatchError`: the pacer rolls back
    this one target's savepoint, keeps what the pass already dispatched, and
    audits the checkout, so no decrypt goes unrecorded (review item 9).
    """

    def __init__(
        self,
        job_engine: JobEngine,
        object_store_factory: Callable[[], ObjectStore],
        vault: VaultService,
        *,
        public_console_url: str | None,
    ) -> None:
        self._job_engine = job_engine
        self._object_store_factory = object_store_factory
        self._object_store: ObjectStore | None = None
        self._vault = vault
        self._public_console_url = public_console_url

    def credential_fingerprint(self, credential: Any) -> str:
        return credential_fingerprint(credential)

    async def dispatch_target(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        run_id: UUID,
        on_behalf_of: UUID,
        credential: Any,
        decision: TargetDecision,
    ) -> RunTargetDispatch:
        address = decision.address
        try:
            checks = await pre_dispatch_checks(
                session,
                org_id,
                decisions=[decision],
                credential=credential,
                kind="install",
                public_console_url=self._public_console_url,
            )
        except PushValidationError:
            return RunTargetDispatch(refusals=("not_installable",))
        if checks.refusals:
            return RunTargetDispatch(
                refusals=tuple(checks.refusals[address]),
                extras=dict(checks.extras.get(address, {})),
            )
        os_name, arch = checks.platforms[address]
        transport, dns_name = checks.transports[address]
        try:
            await _resolve_release(session, os_name, arch)
        except PushValidationError:
            return RunTargetDispatch(refusals=("no_release",), extras={"os": os_name, "arch": arch})

        # THE POINT OF USE (invariant 3): every bound has held for THIS target.
        try:
            raw = await self._vault.checkout(
                session, org_id=org_id, credential_id=credential.id
            )
        except ValueError:
            return RunTargetDispatch(refusals=("credential_unreadable",))
        # From here the secret HAS been decrypted. A failure that is not a
        # named refusal is raised as `RunTargetDispatchError`, so the pacer
        # audits this checkout instead of losing it in a rollback (item 9).
        try:
            return await self._dispatch_checked_out(
                session,
                raw=raw,
                org_id=org_id,
                run_id=run_id,
                on_behalf_of=on_behalf_of,
                credential=credential,
                decision=decision,
                checks=checks,
                os_name=os_name,
                arch=arch,
                transport=transport,
                dns_name=dns_name,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised, typed, below
            raise RunTargetDispatchError(exc) from exc

    async def _dispatch_checked_out(
        self,
        session: AsyncSession,
        *,
        raw: str,
        org_id: UUID,
        run_id: UUID,
        on_behalf_of: UUID,
        credential: Any,
        decision: TargetDecision,
        checks: InstallChecks,
        os_name: str,
        arch: str,
        transport: str,
        dns_name: str | None,
    ) -> RunTargetDispatch:
        address = decision.address
        # Read straight after the checkout, from the row it decrypted: the
        # version this job carries, never a fresher or staler one.
        presented = credential_fingerprint(credential)
        try:
            username, secret = _parse_credential_secret(raw, credential_type=credential.type)
        except (ValueError, PushValidationError):
            return RunTargetDispatch(refusals=("credential_unreadable",))
        if checks.realm is not None:
            try:
                account_realm: str | None = credential_realm.realm_of_account(username)
            except credential_realm.InvalidWindowsDomainAccount:
                account_realm = None
            if account_realm != checks.realm:
                return RunTargetDispatch(
                    refusals=("realm_changed",), extras={"realm": checks.realm}
                )

        if self._object_store is None:
            self._object_store = self._object_store_factory()
        try:
            # A SAVEPOINT, because the enrollment token is written before
            # `JobEngine.create` can refuse a site server awaiting review; the
            # token must not outlive the refusal.
            async with session.begin_nested():
                job, envelope = await _dispatch_install(
                    session,
                    self._job_engine,
                    self._object_store,
                    org_id=org_id,
                    actor_id=on_behalf_of,
                    decision=decision,
                    username=username,
                    secret=secret,
                    credential_name=credential.name,
                    credential_id=credential.id,
                    host_key_fingerprint=checks.confirmed[address],
                    os_name=os_name,
                    arch=arch,
                    transport=transport,
                    dns_name=dns_name,
                    enroll_url=checks.enroll_url,
                    batch_id=run_id,
                    run_id=run_id,
                )
        except DeviceAwaitingReview:
            return RunTargetDispatch(refusals=("site_server_awaiting_review",))
        except PushTargetClosedError:
            # Archived since `pre_dispatch_checks` ran: the savepoint above has
            # rolled the token back.
            return RunTargetDispatch(refusals=("site_archived",))
        return RunTargetDispatch(
            job_id=job.id, envelope=envelope, credential_fingerprint=presented
        )


# --------------------------------------------------------------------------- #
# Result handlers -- run in the GATEWAY process, inside the job's transaction
# --------------------------------------------------------------------------- #


async def record_probe_result(
    session: AsyncSession, job: Job, result: dict[str, Any]
) -> None:
    """Pin what the probe observed (spec §4.4). Carries no secret to record.

    The observed fingerprint becomes a `push_host_key` row awaiting an
    operator's confirmation; only a confirmed key lets a push be dispatched.

    A `winrm` probe observes no key (design §2.4, W3): it reports whether
    5986/5985 answered and whether a KDC was reachable, and that readiness is
    recorded instead. Nothing about it needs a human to confirm.

    **THE SIGNED PARAMS DECIDE WHAT WAS PROBED AND WHERE** (review Minor 3):
    `transport` and `target_address` come from `job.params` -- what the server
    signed -- never from the site server's echo in the result. An echo that
    claimed `ssh` for a job signed `winrm` would otherwise pin a "host key" for
    a Windows target; one that named another address would record it there.
    """
    transport = str(job.params.get("transport") or "ssh")
    address = str(job.params.get("target_address") or "")
    if not address:
        return
    if transport == "winrm":
        device_id = await _batch_device_for_job(session, job)
        await host_key_service.record_readiness(
            session,
            job.org_id,
            address=address,
            device_id=device_id,
            job_id=job.id,
            readiness={
                "winrm_https": result.get("winrm_https") is True,
                "winrm_http": result.get("winrm_http") is True,
                "kdc_reachable": result.get("kdc_reachable") is True,
            },
        )
        return
    fingerprint = str(result.get("host_key_fingerprint") or "").strip()
    if not fingerprint:
        # A probe that reported no key is not evidence of a key. Recorded via
        # the ordinary job result/audit; nothing to pin.
        return
    device_id = await _batch_device_for_job(session, job)
    await host_key_service.record_observation(
        session,
        job.org_id,
        address=address,
        transport=transport,
        fingerprint=fingerprint,
        key_type=str(result.get("host_key_type") or ""),
        device_id=device_id,
        job_id=job.id,
    )


async def record_install_result(
    session: AsyncSession, job: Job, result: dict[str, Any]
) -> None:
    """`push.result` (spec §9). The credential is already scrubbed from
    `jobs.params` by the terminal scrubber; nothing here reads or records it.
    The event is the job's client's -- its site server's, the push's."""
    from app.gateway.dispatch import job_client

    await audit_service.record(
        session,
        org_id=job.org_id,
        actor_type="agent",
        actor_id=None,
        action="push.result",
        client_id=await job_client(session, job.id),
        target_type="push_target",
        target_id=str(result.get("target_address") or job.params.get("target_address") or ""),
        metadata={
            "job_id": str(job.id),
            "outcome": str(result.get("outcome") or ""),
            "os_detected": str(result.get("os_detected") or ""),
            "arch_detected": str(result.get("arch_detected") or ""),
            "detail": str(result.get("detail") or ""),
        },
    )
    # A RUN'S TARGET SETTLES HERE, in the job's own transaction (design §1.4).
    # The freed slot is NOT filled here: this runs in the gateway, which holds
    # no vault (R2). The pacer's next pass fills it. Imported at call time,
    # because `runs` -> `run_list` -> `dispatch` would otherwise be a cycle.
    from app.modules.agent_push.runs import settle_target_for_job

    await settle_target_for_job(session, job)


async def _batch_device_for_job(session: AsyncSession, job: Job) -> UUID | None:
    row = (
        await session.execute(
            select(PushBatchTarget.device_id).where(PushBatchTarget.job_id == job.id)
        )
    ).first()
    return None if row is None else row.device_id

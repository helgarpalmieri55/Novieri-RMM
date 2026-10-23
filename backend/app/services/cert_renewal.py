"""Automatic agent certificate renewal (docs/modules/agent/cert-renewal.md).

Closes OQ-1. Agent certificates are 365 days and are rotated automatically,
pushed from the control plane as two ordinary **signed jobs** — so this adds no
new authenticated surface and no second server→agent instruction path.

# Rotation happens at two-thirds of life, and NOT on expiry

The instruction this was built from said "rotated once expired". That cannot
work and is not what this does.

The certificate IS the agent's mTLS credential. `GatewayServer._handle`
resolves a connection to its `Agent` row by `sha256(DER(leaf))` and closes
**4401** when `cert_not_after < now`, before any application protocol is read.
Once the certificate has expired there is no handshake, so no WebSocket, so no
channel down which a replacement could be pushed. Renewal MUST happen while the
old certificate is still valid.

So rotation begins when less than a third of the TTL remains (day 243 of 365)
and is re-attempted on **every connect** until it succeeds. A machine that is
merely switched off for a few weeks still recovers by itself. A machine offline
for the whole remaining window falls back to re-deployment — the accepted
outcome, reached honestly.

**Do not "fix" this back to on-expiry.** The failure that produces is not a
failed rotation; it is a fleet that cannot be recovered over the network.

# The two legs

    agent.cert.renew    -> {}                     result: {csr_pem}
    agent.cert.install  -> {cert_pem, chain_pem}   result: {cert_fingerprint, not_after}

The private key is generated on the endpoint and never leaves it, at renewal
exactly as at enrollment. Only the CSR travels.

# What the server verifies before signing (§0.5)

`device_id` and `org_id` come from the JOB ROW the server itself wrote, never
from the CSR or from anything else the agent sent. The result carrying the CSR
reaches this module only through `_load_job_for_device`, which discards any
`job_result` naming a job that does not belong to the device the **mTLS
certificate** resolved to. A connected agent therefore cannot obtain a
certificate for another device or another org.
"""

import hmac
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from cryptography import x509
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.ca import AGENT_CERT_TTL_DAYS, CAService, fingerprint_sha256
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import Agent, Job, JobStatus

logger = logging.getLogger("its.cert_renewal")

JOB_TYPE_RENEW = "agent.cert.renew"
JOB_TYPE_INSTALL = "agent.cert.install"
CERT_JOB_TYPES = frozenset({JOB_TYPE_RENEW, JOB_TYPE_INSTALL})

# Rotation starts when this much or less of the certificate's life remains.
#
# A THIRD of the TTL — 122 days of 365, so rotation begins around day 243, which
# is roughly two-thirds of life. Expressed as a share of the TTL rather than a
# literal count so that changing AGENT_CERT_TTL_DAYS moves the trigger with it;
# a hardcoded "122" would be silently wrong (and silently useless on a short
# certificate) the day that default changes. The agent's own
# `enroll.expiringFraction` uses the same one-third rule against the leaf's own
# lifetime, for the same reason.
RENEWAL_REMAINING_DAYS = AGENT_CERT_TTL_DAYS - (AGENT_CERT_TTL_DAYS * 2) // 3

# The last tenth of life. Purely an OPERATOR-FACING distinction (§6): inside
# `renewing` the platform is already trying, and inside `expiring` it has been
# trying for a while without success, which is when a human should look.
EXPIRING_REMAINING_DAYS = AGENT_CERT_TTL_DAYS // 10

# Job states that mean "this rotation is still in flight". A second rotation
# started alongside one of these would race to flip the same row (§0.7).
_OPEN_STATES = (JobStatus.queued, JobStatus.dispatched, JobStatus.running)


class CertRenewalError(Exception):
    """A renewal result that must fail its job rather than change an identity."""


def renewal_due(cert_not_after: datetime, now: datetime | None = None) -> bool:
    """Is this certificate inside the rotation window?

    True for every certificate issued at the old 30-day TTL, unconditionally: 30
    days is never more than the 122-day threshold. That is exactly the intended
    behaviour for certificates already in the field — §0.8 — and it is why
    changing the default TTL needs no back-fill.
    """
    now = now or datetime.now(UTC)
    if cert_not_after.tzinfo is None:
        cert_not_after = cert_not_after.replace(tzinfo=UTC)
    return cert_not_after - now <= timedelta(days=RENEWAL_REMAINING_DAYS)


def cert_state(cert_not_after: datetime | None, now: datetime | None = None) -> str | None:
    """`ok` | `renewing` | `expiring` | `expired`, for the device API (§6).

    `agents.cert_not_after` has been stored since enrollment existed and was
    read by nothing. It now drives rotation, and this is the operator's view of
    the same fact.
    """
    if cert_not_after is None:
        return None
    now = now or datetime.now(UTC)
    if cert_not_after.tzinfo is None:
        cert_not_after = cert_not_after.replace(tzinfo=UTC)
    remaining = cert_not_after - now
    if remaining <= timedelta(0):
        return "expired"
    if remaining <= timedelta(days=EXPIRING_REMAINING_DAYS):
        return "expiring"
    if remaining <= timedelta(days=RENEWAL_REMAINING_DAYS):
        return "renewing"
    return "ok"


async def _open_cert_job(session: AsyncSession, device_id: UUID) -> Job | None:
    return (
        await session.execute(
            select(Job)
            .where(
                Job.target_id == device_id,
                Job.type.in_(CERT_JOB_TYPES),
                Job.status.in_(_OPEN_STATES),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _unconsumed_csr_job(session: AsyncSession, device_id: UUID) -> Job | None:
    """The newest succeeded `agent.cert.renew` whose CSR has not been used.

    "Used" means an `agent.cert.install` job exists that was created AFTER it —
    **whatever that job's outcome**. An install that failed (a malformed
    payload, an endpoint that lost its pending key, a crash mid-swap) therefore
    consumes the CSR too, and the next pass asks for a fresh one. Checking for a
    *succeeded* install instead would sign the same dead CSR on every connect
    forever; that is the entire loop-prevention argument (§0.7).

    Ordering is by `jobs.id`, which is a UUIDv7 and therefore chronological.
    """
    renew = (
        await session.execute(
            select(Job)
            .where(
                Job.target_id == device_id,
                Job.type == JOB_TYPE_RENEW,
                Job.status == JobStatus.succeeded,
            )
            .order_by(Job.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if renew is None or not (renew.result or {}).get("csr_pem"):
        return None
    later_install = (
        await session.execute(
            select(Job.id)
            .where(
                Job.target_id == device_id,
                Job.type == JOB_TYPE_INSTALL,
                Job.id > renew.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return None if later_install is not None else renew


async def advance_renewal(
    session: AsyncSession,
    *,
    agent: Agent,
    ca: CAService,
    job_engine: JobEngine,
    now: datetime | None = None,
) -> Job | None:
    """Move this device's rotation forward by at most one step.

    Returns the job it created, or None. Runs inside the caller's tenant
    session; the caller commits.

    The whole state machine, in order:

      0. revoked            -> nothing (revocation is not undone by a sweep)
      1. not due            -> nothing
      2. rotation in flight -> nothing
      3. unconsumed CSR     -> sign it, dispatch agent.cert.install
      4. otherwise          -> dispatch agent.cert.renew
    """
    now = now or datetime.now(UTC)
    if agent.cert_revoked:
        return None
    if not renewal_due(agent.cert_not_after, now):
        return None
    if await _open_cert_job(session, agent.device_id) is not None:
        return None

    renew_job = await _unconsumed_csr_job(session, agent.device_id)
    if renew_job is not None:
        return await _issue_from_csr(
            session, agent=agent, ca=ca, job_engine=job_engine, renew_job=renew_job
        )
    return await _request_csr(session, agent=agent, job_engine=job_engine)


async def _request_csr(
    session: AsyncSession, *, agent: Agent, job_engine: JobEngine
) -> Job:
    job = await job_engine.create(
        session,
        org_id=agent.org_id,
        type=JOB_TYPE_RENEW,
        target_device_id=agent.device_id,
        params={},
    )
    await audit_service.record(
        session,
        org_id=agent.org_id,
        actor_type="system",
        actor_id=None,
        action="agent.cert.renewal_requested",
        target_type="device",
        target_id=str(agent.device_id),
        metadata={
            "job_id": str(job.id),
            "cert_not_after": agent.cert_not_after.isoformat(),
            "cert_fingerprint": agent.cert_fingerprint,
        },
    )
    return job


async def _issue_from_csr(
    session: AsyncSession,
    *,
    agent: Agent,
    ca: CAService,
    job_engine: JobEngine,
    renew_job: Job,
) -> Job:
    """Sign the CSR the endpoint produced and dispatch the certificate to it.

    THE IDENTITY BINDING COMES FROM `renew_job`, NOT FROM THE CSR. `target_id`
    and `org_id` were chosen by this control plane when it created the renewal
    job for one specific Agent row; the CSR contributes a public key and a proof
    of possession and nothing else. `CAService.sign_csr` overwrites CN and O, so
    a subject in the CSR is not merely unused, it is unusable.
    """
    csr_pem = str((renew_job.result or {}).get("csr_pem", ""))
    try:
        issued = await ca.sign_csr(
            csr_pem,
            device_id=agent.device_id,
            org_id=agent.org_id,
            ttl_days=AGENT_CERT_TTL_DAYS,
        )
    except Exception as exc:  # noqa: BLE001 — a CSR this CA will not sign must
        # not be retried on every connect for the rest of the certificate's
        # life. Audit it, then ask for a fresh one: the new renew job becomes
        # the newest, so the bad CSR is never looked at again.
        await audit_service.record(
            session,
            org_id=agent.org_id,
            actor_type="system",
            actor_id=None,
            action="agent.cert.rotation_failed",
            target_type="device",
            target_id=str(agent.device_id),
            # THE TYPE, NOT THE TEXT. `str(exc)` here put an exception's own
            # message into the AUDIT record -- which `app/api/v1/audit.py`
            # serves to any caller holding `audit.read`. Same leak shape the
            # logging kernel closed for log lines (a CA or OpenBao failure
            # renders a URL, and a URL renders credentials), through a second
            # sink nobody had looked at. The audit entry still names WHAT
            # failed; recovering WHY is the error monitor's job, and its
            # record carries this exception's type and raise site under the
            # same request_id.
            metadata={
                "renew_job_id": str(renew_job.id),
                "reason_type": type(exc).__name__,
            },
        )
        logger.warning("CSR from device %s could not be signed", agent.device_id, exc_info=exc)
        return await _request_csr(session, agent=agent, job_engine=job_engine)

    job = await job_engine.create(
        session,
        org_id=agent.org_id,
        type=JOB_TYPE_INSTALL,
        target_device_id=agent.device_id,
        params={
            "cert_pem": issued.cert_pem,
            "chain_pem": issued.chain_pem,
            "cert_serial": issued.serial,
        },
    )
    await audit_service.record(
        session,
        org_id=agent.org_id,
        actor_type="system",
        actor_id=None,
        action="agent.cert.issued",
        target_type="device",
        target_id=str(agent.device_id),
        metadata={
            "job_id": str(job.id),
            "renew_job_id": str(renew_job.id),
            "cert_serial": issued.serial,
            "cert_fingerprint": issued.fingerprint_sha256,
            "not_after": issued.not_after.isoformat(),
        },
    )
    return job


async def confirm_installed_cert(
    session: AsyncSession, job: Job, result: dict[str, Any]
) -> None:
    """`agent.cert.install` result handler: flip the admission gate.

    THIS IS THE INSTANT THE OLD CERTIFICATE STOPS BEING ACCEPTED (§3.5). Until
    this transaction commits, `agents.cert_fingerprint` still names the old
    certificate and the gateway accepts only that one; after it, the old
    fingerprint resolves to no Agent row and a connection presenting it is
    closed 4401. Nothing else ends the old certificate's acceptance — not its
    own NotAfter (still ~122 days away, which is what gives the endpoint
    something to revert to) and not the CRL (the gateway does not consult it).

    The new identity is taken from `job.params["cert_pem"]` — THE SERVER'S OWN
    BYTES. `result["cert_fingerprint"]` is only an acknowledgement, compared
    against the fingerprint of what was issued; a disagreement raises, which
    `JobEngine.complete` turns into a failed job with the agents row untouched.
    An agent cannot name its own new fingerprint here.
    """
    params = job.params or {}
    cert_pem = str(params.get("cert_pem", ""))
    if not cert_pem:
        raise CertRenewalError(  # i18n-exempt: gateway job-result path, logged only
            "agent.cert.install job carries no cert_pem"
        )

    expected = fingerprint_sha256(cert_pem)
    claimed = str(result.get("cert_fingerprint", "")).strip().lower()
    if not hmac.compare_digest(claimed, expected):
        raise CertRenewalError(  # i18n-exempt: gateway job-result path, logged only
            "the agent acknowledged a certificate this server did not issue for it "
            f"(expected {expected[:16]}..., got {claimed[:16] or '<none>'}...)"
        )

    agent = (
        await session.execute(select(Agent).where(Agent.device_id == job.target_id))
    ).scalar_one_or_none()
    if agent is None:
        raise CertRenewalError(  # i18n-exempt: gateway job-result path, logged only
            f"no agent row for device {job.target_id}"
        )

    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    old_fingerprint, old_serial = agent.cert_fingerprint, agent.cert_serial
    agent.cert_fingerprint = expected
    agent.cert_serial = str(params.get("cert_serial") or format(cert.serial_number, "x"))
    agent.cert_not_after = cert.not_valid_after_utc
    session.add(agent)
    await session.flush()

    await audit_service.record(
        session,
        org_id=job.org_id,
        actor_type="agent",
        actor_id=agent.id,
        action="agent.cert.rotated",
        target_type="device",
        target_id=str(job.target_id),
        metadata={
            "job_id": str(job.id),
            "old_cert_fingerprint": old_fingerprint,
            "old_cert_serial": old_serial,
            "cert_fingerprint": agent.cert_fingerprint,
            "cert_serial": agent.cert_serial,
            "not_after": agent.cert_not_after.isoformat(),
        },
    )


async def advance_for_device(
    app_engine: AsyncEngine,
    job_engine: JobEngine,
    ca: CAService,
    *,
    org_id: UUID,
    device_id: UUID,
) -> dict[str, Any] | None:
    """Run one step of the state machine in its own tenant transaction.

    Returns a signed envelope the CALLER must publish after the commit, or None.
    Building the envelope here and publishing there is the same discipline
    `queue_ui_event` follows: nothing leaves the process describing state that
    has not been made durable.

    Failures are swallowed and logged. This runs on the gateway's connect path
    and on the tail of an agent message; a rotation that cannot be advanced is a
    certificate that will be rotated on the next connect instead, and neither of
    those callers may be taken down by it.
    """
    try:
        async with org_session(org_id, engine=app_engine) as session:
            agent = (
                await session.execute(select(Agent).where(Agent.device_id == device_id))
            ).scalar_one_or_none()
            if agent is None:
                return None
            job = await advance_renewal(session, agent=agent, ca=ca, job_engine=job_engine)
            if job is None:
                return None
            return await job_engine.signed_envelope(job)
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("certificate renewal could not be advanced for device %s", device_id)
        return None

"""Automatic agent certificate renewal (docs/modules/agent/cert-renewal.md).

Closes OQ-1. What these tests assert, and what they deliberately do not:

- **The rotation trigger is two-thirds of life, not expiry.** There is no test
  that renews an expired certificate, because that cannot happen: the gateway
  closes an expired certificate 4401 before any application protocol is read
  (`test_gateway_mtls.py::test_expired_certificate_refused`), so there would be
  no channel to push a replacement down. §0.2.
- **The observable is the `agents` row and the audit log**, never "no
  exception". A rotation that ran and left the admission gate on the old
  fingerprint is the failure this whole design exists to avoid, so every test
  that changes an identity reads the row back.
- **The end-to-end test runs over real mutual TLS** and finishes by dialling
  twice: once with the retired certificate (must be 4401) and once with the new
  one (must be admitted). That is §3.5 -- "exactly when the old certificate
  stops being accepted" -- asserted rather than described.
"""

import asyncio
import hashlib
import json
import socket
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import redis.asyncio as aioredis
import websockets
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.x509.oid import NameOID
from sqlalchemy import select, update

from app.core.ca import AGENT_CERT_TTL_DAYS, LocalCA
from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer, verify_signature
from app.core.tenancy import org_session
from app.gateway.main import GatewayServer
from app.jobs.canonical import canonical_bytes
from app.jobs.engine import JobEngine
from app.models import Agent, AuditEvent, Job, JobStatus
from app.services.cert_renewal import (
    EXPIRING_REMAINING_DAYS,
    JOB_TYPE_INSTALL,
    JOB_TYPE_RENEW,
    RENEWAL_REMAINING_DAYS,
    advance_renewal,
    cert_state,
    confirm_installed_cert,
    renewal_due,
)

REDIS_URL = "redis://localhost:6379/9"

pytestmark = pytest.mark.usefixtures("admin_engine")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_key_and_csr(subject_cn: str | None = None) -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    name = (
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)])
        if subject_cn is not None
        else x509.Name([])
    )
    csr = x509.CertificateSigningRequestBuilder().subject_name(name).sign(key, None)
    key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    return key_pem, csr.public_bytes(Encoding.PEM).decode()


def fingerprint_of(cert_pem: str) -> str:
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return hashlib.sha256(cert.public_bytes(Encoding.DER)).hexdigest()


async def enroll_device(api_env, hostname: str = "renewal-agent") -> dict:
    org_id = await api_env.create_org("cert")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    token = (
        await api_env.client.post(
            "/api/v1/enrollment-tokens",
            json={"name": "cert-test", "expires_in_hours": 1, "max_uses": 1},
            headers=headers,
        )
    ).json()["token"]
    key_pem, csr_pem = make_key_and_csr()
    response = await api_env.client.post(
        "/enroll/v1",
        json={
            "token": token,
            "csr_pem": csr_pem,
            "device_facts": {"hostname": hostname, "os_family": "linux"},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    body["key_pem"] = key_pem
    body["org_id"] = org_id
    body["admin_headers"] = headers
    return body


async def set_not_after(app_engine, org_id: UUID, device_id: UUID, *, days_left: int) -> None:
    """Put the stored `cert_not_after` inside the rotation window.

    The CERTIFICATE stays valid for its full 365 days -- so mutual TLS keeps
    working -- and only the column the rotation reads is moved. That is the
    honest simulation of day 243: `agents.cert_not_after` is what drives
    rotation (§0.6), and forging a short-lived certificate instead would be
    testing the TLS stack rather than the state machine.
    """
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await session.execute(
            update(Agent)
            .where(Agent.device_id == device_id)
            .values(cert_not_after=datetime.now(UTC) + timedelta(days=days_left))
        )


async def agent_row(app_engine, org_id: UUID, device_id: UUID) -> Agent:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        return (
            await session.execute(select(Agent).where(Agent.device_id == device_id))
        ).scalar_one()


async def audit_actions(app_engine, org_id: UUID) -> list[str]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent.action).where(AuditEvent.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )


async def audit_metadata(app_engine, org_id: UUID, action: str) -> dict:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        return (
            await session.execute(
                select(AuditEvent.meta)
                .where(AuditEvent.org_id == org_id, AuditEvent.action == action)
                .order_by(AuditEvent.id.desc())
                .limit(1)
            )
        ).scalar_one()


@pytest.fixture()
def engine_for(api_env):
    return JobEngine(signer=LocalEd25519Signer(api_env.settings.signing_key_path))


@pytest.fixture()
def ca_for(api_env):
    return LocalCA(api_env.settings.ca_dir)


async def step(api_env, engine_for, ca_for, org_id: UUID, device_id: UUID) -> Job | None:
    """One pass of the state machine, in its own transaction."""
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        agent = (
            await session.execute(select(Agent).where(Agent.device_id == device_id))
        ).scalar_one()
        return await advance_renewal(session, agent=agent, ca=ca_for, job_engine=engine_for)


async def complete_job(api_env, engine_for, org_id: UUID, job_id: UUID, result: dict) -> Job:
    """Drive a job to succeeded through the engine, running its result handler."""
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        await engine_for.complete(session, job, state="succeeded", result=result)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        return (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()


# ---------------------------------------------------------------------------
# §0.1 / §0.8 — the TTL, and what happens to certificates already issued at 30
# ---------------------------------------------------------------------------


async def test_newly_issued_agent_certificates_last_a_year(api_env, ca_for) -> None:
    """§0.1. The default is 365, and it is the CA's default -- not something
    every call site has to remember to pass."""
    _, csr_pem = make_key_and_csr()
    issued = await ca_for.sign_csr(csr_pem, device_id=uuid4(), org_id=uuid4())
    lifetime = issued.not_after - datetime.now(UTC)
    assert AGENT_CERT_TTL_DAYS == 365
    assert timedelta(days=364) < lifetime <= timedelta(days=365)


def test_every_certificate_issued_at_the_old_thirty_day_ttl_is_already_due() -> None:
    """§0.8: changing the default affects newly issued certificates ONLY, and
    that needs no back-fill.

    The threshold is "a third of the TTL remains" = 122 days, and a 30-day
    certificate never has more than 30 days left. So every certificate already
    in the field is inside the window by definition and rotates to a 365-day
    one on its first connect after deployment.
    """
    now = datetime.now(UTC)
    for age_days in range(0, 30):
        not_after = now + timedelta(days=30 - age_days)
        assert renewal_due(not_after, now), f"a 30-day cert with {30 - age_days}d left is not due"


def test_the_rotation_window_opens_at_two_thirds_of_life() -> None:
    """§0.2. ~243 days into a 365-day certificate, expressed as a share of the
    TTL so a change to AGENT_CERT_TTL_DAYS moves it."""
    now = datetime.now(UTC)
    assert RENEWAL_REMAINING_DAYS == 122
    assert not renewal_due(now + timedelta(days=RENEWAL_REMAINING_DAYS, seconds=60), now)
    assert renewal_due(now + timedelta(days=RENEWAL_REMAINING_DAYS, seconds=-60), now)
    # 365 - 122 = 243: the day rotation starts.
    assert AGENT_CERT_TTL_DAYS - RENEWAL_REMAINING_DAYS == 243


def test_cert_state_is_the_operator_view_of_the_same_threshold() -> None:
    now = datetime.now(UTC)
    assert cert_state(None) is None
    assert cert_state(now + timedelta(days=300), now) == "ok"
    assert cert_state(now + timedelta(days=100), now) == "renewing"
    assert cert_state(now + timedelta(days=EXPIRING_REMAINING_DAYS - 1), now) == "expiring"
    assert cert_state(now - timedelta(seconds=1), now) == "expired"


# ---------------------------------------------------------------------------
# §3.1 — the state machine
# ---------------------------------------------------------------------------


async def test_a_certificate_outside_the_window_is_left_alone(
    api_env, engine_for, ca_for
) -> None:
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    # freshly enrolled: 365 days, nowhere near the window
    assert await step(api_env, engine_for, ca_for, org_id, device_id) is None
    assert "agent.cert.renewal_requested" not in await audit_actions(api_env.app_engine, org_id)


async def test_the_first_pass_asks_for_a_csr_and_audits_it(api_env, engine_for, ca_for) -> None:
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    await set_not_after(api_env.app_engine, org_id, device_id, days_left=100)

    job = await step(api_env, engine_for, ca_for, org_id, device_id)
    assert job is not None
    assert job.type == JOB_TYPE_RENEW
    assert job.params == {}
    assert job.target_id == device_id
    assert "agent.cert.renewal_requested" in await audit_actions(api_env.app_engine, org_id)


async def test_only_one_rotation_is_in_flight_at_a_time(api_env, engine_for, ca_for) -> None:
    """§0.7. Two concurrent rotations would race to flip the same row."""
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    await set_not_after(api_env.app_engine, org_id, device_id, days_left=100)

    first = await step(api_env, engine_for, ca_for, org_id, device_id)
    assert first is not None
    for _ in range(3):
        assert await step(api_env, engine_for, ca_for, org_id, device_id) is None


async def test_the_second_pass_signs_the_csr_and_dispatches_the_certificate(
    api_env, engine_for, ca_for
) -> None:
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    await set_not_after(api_env.app_engine, org_id, device_id, days_left=100)

    renew = await step(api_env, engine_for, ca_for, org_id, device_id)
    _, csr_pem = make_key_and_csr()
    await complete_job(api_env, engine_for, org_id, renew.id, {"csr_pem": csr_pem})

    install = await step(api_env, engine_for, ca_for, org_id, device_id)
    assert install is not None and install.type == JOB_TYPE_INSTALL
    cert = x509.load_pem_x509_certificate(install.params["cert_pem"].encode())
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == str(device_id)
    assert (cert.not_valid_after_utc - datetime.now(UTC)) > timedelta(days=364)
    assert "agent.cert.issued" in await audit_actions(api_env.app_engine, org_id)

    # the row has NOT moved yet: the gate flips only on confirmation (§3.5)
    row = await agent_row(api_env.app_engine, org_id, device_id)
    assert row.cert_fingerprint == fingerprint_of(enrolled["cert_pem"])


async def test_the_certificate_is_bound_to_the_job_s_device_not_to_anything_the_agent_sent(
    api_env, engine_for, ca_for
) -> None:
    """§0.5 — THE GUARD. A connected agent must not be able to obtain a
    certificate for a different device or a different org.

    The CSR here carries somebody else's device UUID as its subject CN. The
    issued certificate must still name THIS device and THIS org, because the
    binding is read from the job row the server wrote and never from the CSR.
    """
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    await set_not_after(api_env.app_engine, org_id, device_id, days_left=100)

    victim_device, victim_org = uuid4(), uuid4()
    renew = await step(api_env, engine_for, ca_for, org_id, device_id)
    _, csr_pem = make_key_and_csr(subject_cn=str(victim_device))
    await complete_job(api_env, engine_for, org_id, renew.id, {"csr_pem": csr_pem})

    install = await step(api_env, engine_for, ca_for, org_id, device_id)
    cert = x509.load_pem_x509_certificate(install.params["cert_pem"].encode())
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == str(device_id)
    assert cert.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)[0].value == str(org_id)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.UniformResourceIdentifier) == [
        f"its://org/{org_id}/device/{device_id}"
    ]
    assert str(victim_device) not in install.params["cert_pem"]
    assert str(victim_org) not in install.params["cert_pem"]


async def test_a_revoked_agent_is_never_issued_a_fresh_certificate(
    api_env, engine_for, ca_for
) -> None:
    """THE GUARD: revocation must not be undone by a background process.

    Note what this asserts and what it does not: it calls `advance_renewal`
    DIRECTLY, so the gateway's own connect-time refusal of a revoked
    certificate cannot be the thing that makes it pass. That distinction has
    bitten this module twice -- a test that passes because a different check
    caught the case.
    """
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    await set_not_after(api_env.app_engine, org_id, device_id, days_left=100)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await session.execute(
            update(Agent).where(Agent.device_id == device_id).values(cert_revoked=True)
        )

    assert await step(api_env, engine_for, ca_for, org_id, device_id) is None
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        jobs = list((await session.execute(select(Job))).scalars().all())
    assert jobs == []


async def test_a_failed_install_consumes_the_csr_and_a_fresh_one_is_requested(
    api_env, engine_for, ca_for
) -> None:
    """§0.7's loop prevention, and it is the reason the check is "does a later
    install job exist" rather than "did an install job succeed".

    If the CSR were only consumed on SUCCESS, this same dead CSR would be
    re-signed on every connect for the rest of the certificate's life and the
    endpoint would never recover.
    """
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    await set_not_after(api_env.app_engine, org_id, device_id, days_left=100)

    renew = await step(api_env, engine_for, ca_for, org_id, device_id)
    _, csr_pem = make_key_and_csr()
    await complete_job(api_env, engine_for, org_id, renew.id, {"csr_pem": csr_pem})
    install = await step(api_env, engine_for, ca_for, org_id, device_id)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == install.id))).scalar_one()
        await engine_for.complete(session, job, state="failed", result={"error": "boom"})

    again = await step(api_env, engine_for, ca_for, org_id, device_id)
    assert again is not None
    assert again.type == JOB_TYPE_RENEW, "a dead CSR must not be signed a second time"


async def test_a_csr_the_ca_refuses_is_audited_and_replaced(
    api_env, engine_for, ca_for
) -> None:
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    await set_not_after(api_env.app_engine, org_id, device_id, days_left=100)

    renew = await step(api_env, engine_for, ca_for, org_id, device_id)
    await complete_job(api_env, engine_for, org_id, renew.id, {"csr_pem": "-----NOT A CSR-----"})

    replacement = await step(api_env, engine_for, ca_for, org_id, device_id)
    assert replacement is not None and replacement.type == JOB_TYPE_RENEW
    assert "agent.cert.rotation_failed" in await audit_actions(api_env.app_engine, org_id)


async def test_the_rotation_failure_audit_carries_the_exception_TYPE_not_its_text(
    api_env, engine_for, ca_for
) -> None:
    """THE AUDIT LOG IS A SECOND SINK, and it was leaking.

    `cert_renewal` recorded `metadata={"reason": str(exc)}` on a refused CSR.
    `app/api/v1/audit.py` serves that metadata to any caller holding
    `audit.read`, so an exception's own text -- a CA or OpenBao failure
    renders a URL, and a URL renders credentials -- reached a durable,
    API-readable store. The logging kernel closed exactly this shape for log
    lines over four fix waves; nobody had looked at this sink.

    The entry still names WHAT failed (`reason_type`). Recovering WHY is the
    error monitor's job: its record carries the same exception's type and
    raise site under the same `request_id`.
    """
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    await set_not_after(api_env.app_engine, org_id, device_id, days_left=100)

    renew = await step(api_env, engine_for, ca_for, org_id, device_id)
    await complete_job(api_env, engine_for, org_id, renew.id, {"csr_pem": "-----NOT A CSR-----"})
    await step(api_env, engine_for, ca_for, org_id, device_id)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = (
            (
                await session.execute(
                    select(AuditEvent.meta).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "agent.cert.rotation_failed",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows, "no rotation_failed audit event was recorded"
    for metadata in rows:
        assert "reason_type" in metadata
        assert "reason" not in metadata, (
            "the raw exception text is back in the audit record"
        )


# ---------------------------------------------------------------------------
# §3.3 — confirming the install
# ---------------------------------------------------------------------------


async def test_confirmation_flips_the_admission_gate_and_audits_both_fingerprints(
    api_env, engine_for, ca_for
) -> None:
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    await set_not_after(api_env.app_engine, org_id, device_id, days_left=100)
    old_fingerprint = fingerprint_of(enrolled["cert_pem"])

    renew = await step(api_env, engine_for, ca_for, org_id, device_id)
    _, csr_pem = make_key_and_csr()
    await complete_job(api_env, engine_for, org_id, renew.id, {"csr_pem": csr_pem})
    install = await step(api_env, engine_for, ca_for, org_id, device_id)
    new_fingerprint = fingerprint_of(install.params["cert_pem"])

    done = await complete_job(
        api_env,
        engine_for,
        org_id,
        install.id,
        {"cert_fingerprint": new_fingerprint, "not_after": "2027-01-01T00:00:00Z"},
    )
    assert done.status is JobStatus.succeeded

    row = await agent_row(api_env.app_engine, org_id, device_id)
    assert row.cert_fingerprint == new_fingerprint
    assert row.cert_serial == install.params["cert_serial"]
    assert row.cert_not_after > datetime.now(UTC) + timedelta(days=364)

    meta = await audit_metadata(api_env.app_engine, org_id, "agent.cert.rotated")
    assert meta["old_cert_fingerprint"] == old_fingerprint
    assert meta["cert_fingerprint"] == new_fingerprint


async def test_confirming_a_certificate_this_server_did_not_issue_fails_the_job(
    api_env, engine_for, ca_for
) -> None:
    """THE GUARD: the agent's claimed fingerprint is an ACKNOWLEDGEMENT, never
    the new identity.

    An agent that answered with a fingerprint of its own choosing would be
    naming the credential the gateway then admits. It must fail the job and
    leave the row exactly where it was -- a half-rotation is worse than none.
    """
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    await set_not_after(api_env.app_engine, org_id, device_id, days_left=100)
    old_fingerprint = fingerprint_of(enrolled["cert_pem"])

    renew = await step(api_env, engine_for, ca_for, org_id, device_id)
    _, csr_pem = make_key_and_csr()
    await complete_job(api_env, engine_for, org_id, renew.id, {"csr_pem": csr_pem})
    install = await step(api_env, engine_for, ca_for, org_id, device_id)

    forged = hashlib.sha256(b"an identity of my own choosing").hexdigest()
    done = await complete_job(
        api_env,
        engine_for,
        org_id,
        install.id,
        {"cert_fingerprint": forged, "not_after": "2027-01-01T00:00:00Z"},
    )
    assert done.status is JobStatus.failed
    assert "did not issue" in done.result["error"]

    row = await agent_row(api_env.app_engine, org_id, device_id)
    assert row.cert_fingerprint == old_fingerprint, "the admission gate moved on a forged claim"
    assert "agent.cert.rotated" not in await audit_actions(api_env.app_engine, org_id)


async def test_a_confirmation_naming_no_fingerprint_at_all_fails_the_job(
    api_env, engine_for, ca_for
) -> None:
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    await set_not_after(api_env.app_engine, org_id, device_id, days_left=100)

    renew = await step(api_env, engine_for, ca_for, org_id, device_id)
    _, csr_pem = make_key_and_csr()
    await complete_job(api_env, engine_for, org_id, renew.id, {"csr_pem": csr_pem})
    install = await step(api_env, engine_for, ca_for, org_id, device_id)

    done = await complete_job(api_env, engine_for, org_id, install.id, {})
    assert done.status is JobStatus.failed
    row = await agent_row(api_env.app_engine, org_id, device_id)
    assert row.cert_fingerprint == fingerprint_of(enrolled["cert_pem"])


async def test_a_confirmation_for_a_job_carrying_no_certificate_fails(api_env) -> None:
    """`confirm_installed_cert` is reachable only through the job engine, so its
    input is the server's own params -- but it must still refuse rather than
    write a null identity if that ever stops being true."""
    from app.services.cert_renewal import CertRenewalError

    job = Job(
        org_id=uuid4(),
        type=JOB_TYPE_INSTALL,
        target_id=uuid4(),
        params={},
        nonce="n",
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with pytest.raises(CertRenewalError):
        await confirm_installed_cert(None, job, {"cert_fingerprint": "x"})


# ---------------------------------------------------------------------------
# §1 — compatibility, both directions
# ---------------------------------------------------------------------------


async def test_an_agent_that_does_not_know_the_job_type_is_refused_and_keeps_its_certificate(
    api_env, engine_for, ca_for
) -> None:
    """NEW CONTROL PLANE -> OLD AGENT (§1).

    An agent built before this change has neither job type in its capability
    registry, so its runner refuses `unknown_type` before anything runs. The
    refusal must leave the identity untouched and must not wedge the state
    machine: the next pass simply tries again.
    """
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    await set_not_after(api_env.app_engine, org_id, device_id, days_left=100)
    old_fingerprint = fingerprint_of(enrolled["cert_pem"])

    renew = await step(api_env, engine_for, ca_for, org_id, device_id)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == renew.id))).scalar_one()
        await engine_for.refuse(session, job, reason="unknown_type")

    row = await agent_row(api_env.app_engine, org_id, device_id)
    assert row.cert_fingerprint == old_fingerprint
    assert row.cert_not_after < datetime.now(UTC) + timedelta(days=101)

    # and the platform keeps trying rather than giving up on the device
    again = await step(api_env, engine_for, ca_for, org_id, device_id)
    assert again is not None and again.type == JOB_TYPE_RENEW


async def test_a_control_plane_that_never_rotates_leaves_an_agent_exactly_as_it_was(
    api_env, engine_for, ca_for
) -> None:
    """NEW AGENT -> OLD CONTROL PLANE (§1), from the server's side.

    Rotation is entirely server-initiated: nothing in the agent asks for it, so
    there is no request an older control plane would answer with a 404 or a
    validation error. The observable proxy for "an old control plane" is a
    device whose certificate is not yet in the window -- no job is created, and
    the enrollment path is untouched.

    The agent's half of this direction is `TestAnAgentAgainstAServerThatNeverRotatesIsUnchanged`.
    """
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])

    assert await step(api_env, engine_for, ca_for, org_id, device_id) is None
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert list((await session.execute(select(Job))).scalars().all()) == []
    row = await agent_row(api_env.app_engine, org_id, device_id)
    assert row.cert_fingerprint == fingerprint_of(enrolled["cert_pem"])


async def test_neither_leg_is_dispatchable_through_the_kernel_jobs_route(api_env) -> None:
    """§1. An operator-authored `agent.cert.install` would push an arbitrary
    certificate onto an endpoint -- the exact attack this design closes."""
    enrolled = await enroll_device(api_env)
    org_id = enrolled["org_id"]
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    for job_type, params in (
        (JOB_TYPE_RENEW, {}),
        (JOB_TYPE_INSTALL, {"cert_pem": "x", "chain_pem": "y", "cert_serial": "z"}),
    ):
        response = await api_env.client.post(
            "/api/v1/jobs",
            json={"type": job_type, "device_id": enrolled["device_id"], "params": params},
            headers=headers,
        )
        assert response.status_code == 403, (job_type, response.text)


# ---------------------------------------------------------------------------
# §6 — what an operator sees
# ---------------------------------------------------------------------------


async def test_the_device_api_now_reads_cert_not_after(api_env) -> None:
    """It was stored since enrollment existed and read by nothing."""
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], enrolled["device_id"]
    headers = enrolled["admin_headers"]

    body = (await api_env.client.get(f"/api/v1/devices/{device_id}", headers=headers)).json()
    assert body["cert_state"] == "ok"
    assert body["cert_not_after"] is not None

    await set_not_after(api_env.app_engine, org_id, UUID(device_id), days_left=100)
    body = (await api_env.client.get(f"/api/v1/devices/{device_id}", headers=headers)).json()
    assert body["cert_state"] == "renewing"

    listed = (await api_env.client.get("/api/v1/devices", headers=headers)).json()
    assert listed["items"][0]["cert_state"] == "renewing"


# ---------------------------------------------------------------------------
# End to end over real mutual TLS
# ---------------------------------------------------------------------------


@pytest.fixture()
async def gateway(api_env, tmp_path: Path):
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    await redis.flushdb()
    ca = LocalCA(api_env.settings.ca_dir)
    key_pem, cert_pem = ca.issue_server_cert(["localhost"])
    (tmp_path / "gw.key").write_text(key_pem)
    (tmp_path / "gw.crt").write_text(cert_pem)

    port = _free_port()
    server = GatewayServer(
        settings=api_env.settings,
        app_engine=api_env.app_engine,
        admin_engine=api_env.admin_engine,
        job_engine=JobEngine(signer=LocalEd25519Signer(api_env.settings.signing_key_path)),
        redis=redis,
    )
    handle = await server.start(
        host="localhost",
        port=port,
        certfile=str(tmp_path / "gw.crt"),
        keyfile=str(tmp_path / "gw.key"),
        ca_certs=str(api_env.settings.ca_dir / "root.crt"),
    )
    yield server, port
    handle.close()
    await handle.wait_closed()
    await server.stop()
    await redis.aclose()


def _client_ssl(tmp_path: Path, cert_pem: str, key_pem: str, ca_pem: str, name: str):
    (tmp_path / f"{name}.crt").write_text(cert_pem)
    (tmp_path / f"{name}.key").write_text(key_pem)
    (tmp_path / f"{name}-ca.pem").write_text(ca_pem)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_cert_chain(tmp_path / f"{name}.crt", tmp_path / f"{name}.key")
    ctx.load_verify_locations(tmp_path / f"{name}-ca.pem")
    return ctx


async def test_a_whole_rotation_over_mutual_tls(api_env, gateway, tmp_path: Path) -> None:
    """Both legs, on one connection, then the admission flip proved by dialling.

    This is the test that would have caught OQ-1 if it had existed: it runs the
    real gateway, the real CA and the real job engine, and it finishes by
    presenting BOTH certificates and asserting which one is admitted.
    """
    server, port = gateway
    enrolled = await enroll_device(api_env)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    await set_not_after(api_env.app_engine, org_id, device_id, days_left=100)

    new_key_pem, new_csr_pem = make_key_and_csr()
    ctx = _client_ssl(
        tmp_path, enrolled["cert_pem"], enrolled["key_pem"], enrolled["ca_bundle_pem"], "old"
    )
    async with websockets.connect(f"wss://localhost:{port}/agent", ssl=ctx) as ws:
        # leg 1 arrives unprompted, drained at connect
        renew = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert renew["job"]["type"] == JOB_TYPE_RENEW
        # invariant #1: it is a signed job like any other
        assert verify_signature(
            enrolled["job_signing_public_key_pem"],
            canonical_bytes(renew["job"]),
            renew["sig"],
        )
        assert renew["job"]["device"] == str(device_id)
        await ws.send(
            json.dumps(
                {
                    "v": 1,
                    "kind": "job_result",
                    "job_id": renew["job"]["id"],
                    "state": "succeeded",
                    "result": {"csr_pem": new_csr_pem},
                }
            )
        )

        # leg 2 follows down the SAME connection. The first connection after
        # enrolment was also handed an `inventory.collect`, drained after leg 1.
        install = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if install["job"]["type"] == "inventory.collect":
            install = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert install["job"]["type"] == JOB_TYPE_INSTALL
        assert verify_signature(
            enrolled["job_signing_public_key_pem"],
            canonical_bytes(install["job"]),
            install["sig"],
        )
        new_cert_pem = install["job"]["params"]["cert_pem"]
        new_fingerprint = fingerprint_of(new_cert_pem)
        await ws.send(
            json.dumps(
                {
                    "v": 1,
                    "kind": "job_result",
                    "job_id": install["job"]["id"],
                    "state": "succeeded",
                    "result": {
                        "cert_fingerprint": new_fingerprint,
                        "not_after": "2027-01-01T00:00:00Z",
                    },
                }
            )
        )
        await asyncio.sleep(0.5)

    row = await agent_row(api_env.app_engine, org_id, device_id)
    assert row.cert_fingerprint == new_fingerprint

    # §3.5: from the confirming commit, the OLD certificate is refused. The
    # gateway closes AFTER the handshake (it resolves the fingerprint once the
    # WebSocket is up), so the verdict is observed on the first read -- which is
    # also exactly how the Go agent sees it, and why `transport.terminalError`
    # keys on the close code rather than on a dial failure.
    async with websockets.connect(f"wss://localhost:{port}/agent", ssl=ctx) as stale:
        with pytest.raises(websockets.exceptions.ConnectionClosed) as refused:
            await asyncio.wait_for(stale.recv(), timeout=5)
    assert refused.value.rcvd.code == 4401

    # ...and the NEW one is admitted, with no further rotation dispatched.
    new_ctx = _client_ssl(
        tmp_path, new_cert_pem, new_key_pem, enrolled["ca_bundle_pem"], "new"
    )
    async with websockets.connect(f"wss://localhost:{port}/agent", ssl=new_ctx) as ws:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(ws.recv(), timeout=1.5)

    actions = await audit_actions(api_env.app_engine, org_id)
    for expected in (
        "agent.cert.renewal_requested",
        "agent.cert.issued",
        "agent.cert.rotated",
    ):
        assert expected in actions, expected

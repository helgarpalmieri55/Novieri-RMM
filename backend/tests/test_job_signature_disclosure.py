"""What the job console may be told about a job's signature (audit
`docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`, rec. 2 —
finding B43, "the 'Signed job engine' never shows any signature information").

THE TWO ASKS ARE NOT THE SAME ASK. *Showing that a job was signed* and
*handing over the material that makes it executable* look alike in a response
model and are worlds apart in exposure. `jobs.signature` is on the reporting
module's `_NEVER_A_COLUMN` (`app/modules/reporting/registry.py`) precisely
because it is the signed-job engine's own authorization material: the envelope
an agent verifies is `payload + sig`, so the signature IS the capability, and
`jobs.read` — held by `auditor` and `read-only` — is a far weaker permission
than the one that authorized the dispatch.

So the route answers the first ask and refuses the second. It returns whether
a signature is on record, the algorithm that verifies it, and a SHA-256 digest
that names the signature stably without being it. The raw base64 never leaves
the process.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.attestation import SIGNATURE_ALGORITHM, signature_digest
from app.models import Device, Job


async def _device(api_env, org_id: UUID, hostname: str = "sig-host") -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname=hostname, os_family="windows")
        session.add(device)
        await session.commit()
        return device.id


async def _dispatch(api_env, org_id: UUID, device_id: UUID, headers: dict[str, str]) -> dict:
    created = await api_env.client.post(
        "/api/v1/jobs",
        json={"type": "hello", "device_id": str(device_id), "params": {}},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    body: dict = created.json()
    return body


# --------------------------------------------------------------------------- #
# What the console gains
# --------------------------------------------------------------------------- #


async def test_a_dispatched_job_says_it_is_signed_and_how(api_env) -> None:
    """B43's whole complaint: a console calling itself a signed-job engine
    showed nothing about any signature. `signed`, `signature_algorithm` and
    `signature_digest` are the three facts a reader needs to say "this job
    carries a server signature, made with Ed25519, and here is its name"."""
    org_id = await api_env.create_org("jobsig")
    device_id = await _device(api_env, org_id)
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    job = await _dispatch(api_env, org_id, device_id, headers)

    assert job["signed"] is True
    assert job["signature_algorithm"] == "ed25519"
    assert isinstance(job["signature_digest"], str)
    assert len(job["signature_digest"]) == 64
    assert set(job["signature_digest"]) <= set("0123456789abcdef")

    detail = await api_env.client.get(f"/api/v1/jobs/{job['id']}", headers=headers)
    assert detail.json()["signature_digest"] == job["signature_digest"]

    listing = await api_env.client.get("/api/v1/jobs", headers=headers)
    row = next(j for j in listing.json()["items"] if j["id"] == job["id"])
    assert row["signed"] is True
    assert row["signature_digest"] == job["signature_digest"]


async def test_the_digest_names_the_signature_without_being_it(api_env) -> None:
    """The digest is SHA-256 over the stored base64 signature. It is stable,
    comparable between the list and the detail view, and one-way: nothing in
    the response reconstructs the bytes an agent verifies.

    The assertion that matters is the second one — the raw signature must not
    appear ANYWHERE in the body, under any key. `test_api_routes.py` already
    pins the absence of a `signature` KEY; a digest field added carelessly
    could satisfy that and still print the signature."""
    org_id = await api_env.create_org("jobsig")
    device_id = await _device(api_env, org_id)
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    job = await _dispatch(api_env, org_id, device_id, headers)

    async with AsyncSession(api_env.admin_engine) as session:
        stored = (
            await session.execute(select(Job.signature).where(Job.id == UUID(job["id"])))
        ).scalar_one()
    assert stored is not None

    assert job["signature_digest"] == hashlib.sha256(stored.encode("ascii")).hexdigest()

    detail = await api_env.client.get(f"/api/v1/jobs/{job['id']}", headers=headers)
    assert stored not in detail.text
    assert "signature" not in detail.json()
    assert "nonce" not in detail.json()


async def test_a_row_carrying_no_signature_says_so_rather_than_asserting_one(
    api_env,
) -> None:
    """`jobs.signature` is nullable, so `signed` has to be read off the column
    rather than hard-wired true. A response that claimed every job was signed
    would be exactly the invented field the closeout refused to build."""
    org_id = await api_env.create_org("jobsig")
    device_id = await _device(api_env, org_id)
    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        job = Job(
            org_id=org_id,
            type="hello",
            target_id=device_id,
            params={},
            signature=None,
            nonce=str(uuid4()),
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    body = (await api_env.client.get(f"/api/v1/jobs/{job_id}", headers=headers)).json()

    assert body["signed"] is False
    assert body["signature_digest"] is None
    assert body["signature_algorithm"] is None


async def test_a_re_signed_job_gets_a_different_digest(api_env) -> None:
    """`JobEngine.resign_for_retry` mints a fresh nonce and signs again, so the
    digest is a per-ATTEMPT name and not a per-job one. If it did not move, a
    console showing it would let a reader believe the envelope in flight is the
    one they looked at before the retry."""
    org_id = await api_env.create_org("jobsig")
    device_id = await _device(api_env, org_id)
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    job = await _dispatch(api_env, org_id, device_id, headers)
    before = job["signature_digest"]

    engine = api_env.app.state.job_engine
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        row = (
            await session.execute(select(Job).where(Job.id == UUID(job["id"])))
        ).scalar_one()
        await engine.resign_for_retry(session, row)
        await session.commit()

    after = (
        await api_env.client.get(f"/api/v1/jobs/{job['id']}", headers=headers)
    ).json()["signature_digest"]
    assert after != before


# --------------------------------------------------------------------------- #
# The derivation itself
# --------------------------------------------------------------------------- #


def test_the_digest_helper_is_total_and_one_way() -> None:
    assert signature_digest(None) is None
    assert signature_digest("") is None
    digest = signature_digest("c2lnbmF0dXJl")
    assert digest == hashlib.sha256(b"c2lnbmF0dXJl").hexdigest()
    assert "c2lnbmF0dXJl" not in (digest or "")


def test_the_algorithm_named_is_the_one_the_signer_actually_uses() -> None:
    """`SIGNATURE_ALGORITHM` is a constant because the SCHEMA stores no
    per-job algorithm, and it is honest because the platform has exactly one
    signer: `app/core/signing.py` offers `LocalEd25519Signer` and
    `OpenBaoTransitSigner`, both Ed25519, and `verify_signature` loads an
    `Ed25519PublicKey` or returns False. The day a second algorithm exists,
    this constant becomes a lie and the column has to carry the fact — which
    is what this test is here to force somebody to notice."""
    import inspect

    from app.core import signing

    source = inspect.getsource(signing)
    assert SIGNATURE_ALGORITHM == "ed25519"
    assert "Ed25519PrivateKey" in source
    assert not any(
        name in source for name in ("RSAPrivateKey", "EllipticCurvePrivateKey", "ECDSA")
    ), "a second signing algorithm exists; `signature_algorithm` must stop being a constant"

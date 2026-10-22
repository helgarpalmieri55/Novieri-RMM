"""What a `jobs.read` holder may be told about a job's signature.

Finding B43 of the 2026-08-19 UX audit is that a console calling itself a
signed-job engine shows no signature information at all, and the closeout
recorded why nothing was built: `JobOut` carried no such field, so a signature
panel would have meant inventing one. This module is the derivation that makes
the panel honest.

WHAT IS DELIBERATELY NOT RETURNED, and why the distinction is not fussiness.
An agent executes `{"job": <payload>, "sig": <signature>}` after verifying the
second over the canonical bytes of the first (`app/jobs/engine.py::
signed_envelope`, `docs/05-agent-spec.md`). The signature is therefore not a
description of an authorization, it IS the authorization — the one artefact
invariant #1 exists to protect. `jobs.read` is held by `auditor` and
`read-only` (migration 0003); the permission that authorized the dispatch is
`jobs.dispatch`, and several job types are refused even to that
(`app/jobs/direct_dispatch.py`). Echoing the signature to the weaker
permission would hand every reader the engine's own material, which is the
reasoning `app/modules/reporting/registry.py` already wrote down when it put
`jobs.signature` and `jobs.nonce` on `_NEVER_A_COLUMN`. Nothing here changes
that: the raw signature still leaves the process only inside a dispatch
envelope addressed to one agent.

WHAT IS RETURNED INSTEAD IS A NAME FOR IT. `signature_digest` is SHA-256 over
the stored base64 signature — one-way, stable, and comparable, so a reader can
see that the detail view and the list agree, that a job was re-signed for a
retry, and that a signature exists at all. That answers "is this engine doing
what it claims", which is what B43 asked, without answering "what would I send
to make an agent act".

THERE IS NO KEY IDENTIFIER HERE, AND THAT IS A SCHEMA GAP RATHER THAN A
CHOICE. `jobs` stores no signing-key id or version, so any key identifier put
on a job row would describe the key the API holds NOW, not the one that signed
that row — which after a rotation is a confident false statement about the
exact thing an operator would be checking. Closing it properly means a column
written at signing time, i.e. a migration and a change to the signing path,
and it is recorded as still-open rather than approximated.
"""

import hashlib

#: The algorithm every signature in this platform is made with.
#:
#: A CONSTANT, BECAUSE THE SCHEMA HOLDS NO PER-JOB ALGORITHM, and an honest one
#: because there is exactly one signer: `app/core/signing.py` offers
#: `LocalEd25519Signer` and `OpenBaoTransitSigner`, both Ed25519, and
#: `verify_signature` refuses anything that does not load as an
#: `Ed25519PublicKey`. `tests/test_job_signature_disclosure.py` asserts that no
#: second algorithm has appeared in that module, so the day one does, this
#: constant fails rather than quietly mislabelling old rows.
SIGNATURE_ALGORITHM = "ed25519"


def signature_digest(signature: str | None) -> str | None:
    """A stable, one-way name for `signature` — never the signature itself.

    `None` for a row that carries none: `jobs.signature` is nullable, and a
    digest of the empty string would be a constant that reads as a real one.
    """
    if not signature:
        return None
    return hashlib.sha256(signature.encode("ascii")).hexdigest()

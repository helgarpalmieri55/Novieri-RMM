#!/usr/bin/env python3
"""Live measurement of the backup READ grant against REAL object storage.

WHY THIS EXISTS AS A SEPARATE SCRIPT FROM `verify_backup_run.py`
================================================================

`scripts/verify_backup_run.py` measures `backup.run` and `backup.restore`
against the full `deploy/docker-compose.yml` stack. Its restore leg carried
this, verbatim, from the day it was written:

    THE RESTORE (READ) LEG BELOW HAS NEVER BEEN EXECUTED, IN ANY ENVIRONMENT

and the stated cause was that the environment had no Docker daemon, so the
compose stack could not come up. That cause is real and still true here.

**But most of what the read leg asserts does not need the stack.** MinIO is a
single static binary. The read grant is signed by the control plane's own
`ObjectStore.presign_backup_read` and redeemed by an HTTP GET; whether real
S3 honours it, and whether real S3 refuses a reused signature, are properties
of MinIO and SigV4 alone -- no Postgres, no Redis, no gateway, no agent, no
snapshot provider, and none of the two blockers that stop `backup.run`
completing end to end.

So the parts that could be measured were separated out and measured. This
script is what ran; `verify_backup_run.py`'s docstring is corrected to say
which half of its read leg has now executed and which half still has not.

WHAT THIS DRIVES
================

1. A grant minted by `presign_backup_read` is honoured by real MinIO (200)
   and returns the exact bytes that were written.
2. A batch of N suffixes yields N grants, each resolving to its OWN object --
   the failure this catches is every grant in a batch pointing at one key.
3. That same signature retargeted at a DIFFERENT key is refused (403), and
   the victim key is confirmed readable under its own grant, so the 403 is
   SigV4 verification rather than a 404 wearing a 403's clothes.

   THIS IS GRANT-REUSE RESISTANCE, NOT PREFIX ENFORCEMENT, and the difference
   matters enough to state twice. The WRITE grant carries a signed
   `starts-with $key` policy condition that S3 itself enforces. Reads have no
   equivalent: SigV4 query signing cannot express a prefix, which is the whole
   reason `presign_backup_read` signs each object individually
   (`docs/modules/backup/spec.md` §6.1 item 1). The read prefix boundary is
   APPLICATION-LEVEL -- it lives in `_backup_key` deriving the prefix from ids
   the caller cannot choose -- and no object store can test it for us.
4. A grant expires. Signed with a 1-second lifetime, it is refused after it
   elapses. `MAX_PRESIGNED_TTL_S` bounds the other end; this measures that the
   lower end is real rather than advisory.
5. One unsafe suffix refuses the WHOLE batch. Asserted against a live store so
   "nothing was signed" means no usable URL exists, not merely that an
   exception type was raised.

WHAT THIS DOES NOT DRIVE, and must not be reported as driven
============================================================

* The byte-for-byte restored-tree comparison. That needs the agent, a snapshot
  provider, and the gateway, and `backup.run` does not complete end to end --
  see `verify_backup_run.py`'s blockers 1 and 2, which this script neither
  reproduces nor resolves.
* The `ceil(distinct chunks / 256)` batching count on the wire. That is
  agent-side behaviour observed on a live connection.
* Anything about the write leg.
* TLS, Traefik, the operator hostname, or the public presign endpoint. This
  talks to MinIO directly over plain HTTP on loopback.

RUNNING IT
==========

    # any real MinIO will do; it needs no Docker
    curl -sSLo /tmp/minio https://dl.min.io/server/minio/release/linux-amd64/minio
    chmod +x /tmp/minio
    MINIO_ROOT_USER=minioadmin MINIO_ROOT_PASSWORD=minioadmin \
        /tmp/minio server /tmp/miniodata --address 127.0.0.1:9100 &

    backend/.venv/bin/python scripts/verify_backup_read_grants.py

Every step prints PASS or FAIL and the script exits non-zero if any failed.

NOT IN CI, and that is a limitation rather than a decision: it needs a MinIO
process. A regression in what it checks returns silently until somebody runs
it. Said plainly because this project has shipped named-but-unrun checks
before, and the docstring above is a correction of exactly that.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile
import time
import urllib.parse
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))

import requests
from app.core.config import Settings
from app.core.storage import ObjectStore

# Loopback must not go through an outbound proxy: an intercepting proxy would
# rewrite the request and the signature check would fail for the wrong reason.
NOPROXY = {"http": None, "https": None}

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", default="http://127.0.0.1:9100")
    ap.add_argument("--bucket", default="it-suite-live")
    ap.add_argument("--access-key", default="minioadmin")
    ap.add_argument("--secret-key", default="minioadmin")
    args = ap.parse_args()

    unreachable = "postgresql+asyncpg://u:p@127.0.0.1:1/db"
    tmp = pathlib.Path(tempfile.mkdtemp())
    settings = Settings(
        database_url=unreachable,
        database_admin_url=unreachable,
        database_portal_url=unreachable,
        oidc_issuer="https://idp.invalid",
        oidc_jwks_url="https://idp.invalid/jwks",
        ca_dir=tmp / "ca",
        signing_key_path=tmp / "job-signing.key",
        s3_endpoint=args.endpoint,
        s3_bucket=args.bucket,
        s3_access_key=args.access_key,
        s3_secret_key=args.secret_key,
    )
    store = ObjectStore(settings)
    client = store._client
    try:
        client.create_bucket(Bucket=args.bucket)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass

    org, repo = uuid.uuid4(), uuid.uuid4()
    # Distinct lengths as well as distinct contents: a bug that returned the
    # wrong object would otherwise be invisible if both bodies were the same
    # size and the check only compared lengths.
    bodies = {
        "chunks/aa/aaaa1111": b"the first chunk's bytes",
        "chunks/bb/bbbb2222": b"a SECOND chunk, different length and content",
    }
    for suffix, body in bodies.items():
        client.put_object(
            Bucket=args.bucket,
            Key=store._backup_key(org, repo, suffix),
            Body=body,
        )

    # --- 1 + 2 ---
    grants = store.presign_backup_read(org, repo, list(bodies), expires_in=900)
    check("batch returns one grant per suffix", len(grants) == len(bodies), f"{len(grants)}")

    honoured = True
    for g in grants:
        r = requests.get(g["url"], proxies=NOPROXY, timeout=30)
        if r.status_code != 200 or r.content != bodies[g["suffix"]]:
            honoured = False
            print(f"      {g['suffix']}: HTTP {r.status_code}, {len(r.content)} bytes")
    check("every grant is honoured by real MinIO and returns its own bytes", honoured)

    # --- 3: retarget the signature at a different key ---
    # Only the PATH changes; the query string, and therefore the signature,
    # stays byte-identical. SigV4 signs the canonical URI, so this must fail.
    first, second = grants[0], grants[1]
    parsed = urllib.parse.urlsplit(first["url"])
    retargeted = urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            urllib.parse.urlsplit(second["url"]).path,
            parsed.query,
            parsed.fragment,
        )
    )
    r = requests.get(retargeted, proxies=NOPROXY, timeout=30)
    check("a grant retargeted at another key is refused", r.status_code == 403, f"HTTP {r.status_code}")

    r_own = requests.get(second["url"], proxies=NOPROXY, timeout=30)
    check(
        "  ...and the retarget victim reads fine under its own grant",
        r_own.status_code == 200,
        f"HTTP {r_own.status_code} -- so the 403 is signature verification, not a 404",
    )

    # --- 4: expiry ---
    short = store.presign_backup_read(org, repo, ["chunks/aa/aaaa1111"], expires_in=1)[0]
    r_before = requests.get(short["url"], proxies=NOPROXY, timeout=30)
    check("a 1s grant works while it is alive", r_before.status_code == 200, f"HTTP {r_before.status_code}")
    time.sleep(3)
    r_after = requests.get(short["url"], proxies=NOPROXY, timeout=30)
    check("  ...and is refused once it has expired", r_after.status_code == 403, f"HTTP {r_after.status_code}")

    # --- 5: one bad suffix refuses the whole batch ---
    refused = ""
    try:
        store.presign_backup_read(org, repo, ["chunks/aa/aaaa1111", "../../escape"], expires_in=900)
    except Exception as exc:  # noqa: BLE001 - any refusal is the assertion
        refused = type(exc).__name__
    check("a traversal suffix refuses the WHOLE batch", bool(refused), refused or "NOT REFUSED")

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

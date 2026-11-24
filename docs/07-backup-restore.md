# Backup & restore (control plane)

This is the disaster-recovery runbook for the **control plane** — the compose
stack in `deploy/`. It is not about the Backup *module* (the fleet file-backup
engine, `docs/modules/backup.md`); that module's data is *part of* what this
runbook protects.

Two things in this stack are not ordinary data and get their own sections
below, because getting them wrong is unrecoverable rather than inconvenient:

- **The audit log is hash-chained and HMAC-authenticated.** Restoring the
  database without the same HMAC key leaves every audit event unverifiable
  forever. See [The audit chain](#the-audit-chain).
- **The internal CA and the job-signing key live inside OpenBao.** Losing them
  means every enrolled agent must re-enroll. See
  [The internal CA and signing key](#the-internal-ca-and-signing-key).

> **Verification status.** The PostgreSQL dump/restore path, the Docker-volume
> tar path, the post-restore privilege re-application, and the audit chain
> check in this document were executed end to end against this stack -- the
> chain check then as a `curl` with a bearer token. Its replacement in §4 was
> run on 2026-11-19 as `python -` reading the same text, outside a container,
> against a migrated database holding HMAC-keyed events (it answered `ok:
> true`, and `ok: false` with another key), not yet inside a running stack,
> and is marked. The OpenBao section was **not** executed (no initialized
> OpenBao was available); it is written from the configuration in
> `deploy/openbao/` and is marked where that matters. Treat unmarked
> commands as tested and marked ones as needing a rehearsal before you rely
> on them.

---

## 1. What must be backed up

| # | State | Where it lives | Lose it and… |
|---|---|---|---|
| 1 | Application database (`itsuite`) | `postgres-data` volume | everything: devices, tickets, jobs, audit log, vault *ciphertext* |
| 2 | The old identity provider's database (`keycloak`), only on a host upgraded from before own authentication phase 4 | same volume, same cluster | the realm that release used; only a backup taken before an upgrade with `ITS_BACKUP_OLD_IDENTITY_DB=1` holds it (`deploy.sh`, and the manual upgrade's step 1, set it), and nothing reads it after phase 4 (`deploy/README.md`, "Retiring the old identity provider's data") |
| 3 | Object storage | `minio-data` volume | backup repositories, deploy artifacts, OS images, attachments |
| 4 | **OpenBao storage** | `openbao-data` volume | the internal CA, the job-signing key, **every vault DEK** — see §5 |
| 5 | **OpenBao unseal keys + root token** | *operator-held, outside the stack* | the volume in #4 becomes undecryptable ballast |
| 6 | **Audit HMAC key** (and anything else on that volume — today, an optional AI provider API key) | `its-secrets` volume | the restored audit chain can never be verified — see §4; a lost provider key must be reissued by its vendor |
| 7 | CA material on disk | `its-ca` volume | the gateway's client-CA trust anchor (and, with `ITS_CA_BACKEND=local`, the CA private key itself) |
| 8 | `deploy/.env` | the host, gitignored | DB passwords, OpenBao token, MinIO credentials, the console address |
| 9 | Gateway server cert | `its-gateway-tls` volume | re-issuable from the CA with `deploy/scripts/issue-gateway-cert.py`; not critical if #4/#7 survive |
| 10 | Operator TLS cert for `:443` | `traefik-acme` volume (Traefik's ACME store, since 2026-11-23) | not key material, and not backed up: a lost volume costs one new certificate, which Traefik asks Let's Encrypt for at its next start (at most 5 a week for the same name) |

Items **4, 5, 6, 7 and 8 are key material, not data.** They must go to a
different medium and a different trust domain from the database dump — an
attacker (or a fire) that gets the database backup and the key backup together
gets the plaintext of every stored credential in the fleet.

**What is NOT backed up, deliberately:** `valkey-data` (the `redis` service,
Valkey since 2026-09-23: Celery broker state and gateway pub/sub — transient;
a lost queue costs at most one sweep cycle, and every sweep is idempotent by
design).

---

## 2. Taking a backup

Run from `deploy/`. `$BK` is a directory on a **different** host or volume.

### 2.1 PostgreSQL

```sh
BK=/srv/backup/$(date -u +%Y%m%dT%H%M%SZ) && mkdir -p "$BK"

docker compose exec -T postgres pg_dump -U its_admin -d itsuite  -Fc > "$BK/itsuite.dump"
docker compose exec -T postgres pg_dumpall -U its_admin --globals-only > "$BK/globals.sql"
```

Notes that matter:

- `its_admin` is a **superuser** in this deployment, so the dump is not
  filtered by row-level security. Dumping as `app_user` would silently produce
  an empty or partial dump — RLS applies to it and no `app.current_org` GUC is
  set. Always dump as `its_admin`.
- `globals.sql` **contains the SCRAM password hashes** of `its_admin`,
  `app_user`, and `portal_user`. It is a secret. Encrypt it, or skip it
  entirely — the restore path in §3 does not need it, because
  `init-app-user.sh` recreates the roles from `.env`.
- `-Fc` (custom format) is required for the `pg_restore --clean --if-exists`
  used in §3.
- **The old identity provider's database is left out**, as the nightly and
  off-site backups leave it out (operator decision K1, 2026-11-15). Only a
  backup taken before an upgrade with `ITS_BACKUP_OLD_IDENTITY_DB=1` holds
  it; `deploy.sh`, and the manual upgrade's step 1, set it
  (`deploy/README.md`).

### 2.2 Object storage, OpenBao, and the key volumes

Volume contents are copied through a throwaway container. **Stop the writer
first** — a live tar of an active MinIO or OpenBao data directory is not a
consistent snapshot.

```sh
docker compose stop minio openbao api gateway worker

for v in minio-data openbao-data its-ca its-secrets its-gateway-tls; do
  docker run --rm -v "it-suite_$v:/data:ro" -v "$BK:/backup" alpine \
    tar czf "/backup/$v.tgz" -C /data .
done

docker compose start minio openbao api gateway worker
```

For a large MinIO, `mc mirror` to a second bucket/host is the better tool and
does not require downtime; the tar above is the simple, verified path for a
single-node install.

### 2.3 Configuration and operator-held secrets

```sh
cp deploy/.env "$BK/env"          # encrypt this
```

And, **once, at install time, not per-backup**: store the OpenBao unseal keys
and root token from `bao operator init` in a separate secret store (a password
manager or sealed envelope). They are printed once and never again. A backup
of `openbao-data` without them restores nothing.

### 2.4 Encrypt and ship

`read` asks for your `age` public key (the `age1…` line `age-keygen`
printed); `gpg -e` works the same way if that is what you use:

```sh
read -rp 'age recipient (age1...): ' AGE_RECIPIENT && \
  tar czf - -C "$BK" . | age -r "${AGE_RECIPIENT:?no recipient}" > "$BK.tar.gz.age"
```

---

## 3. Restoring into a clean stack

This is the verified path. It restores into a **freshly initialized** compose
stack, so the roles come from `.env` (via `init-app-user.sh`) rather than from
`globals.sql` — which avoids "role already exists" and keeps the passwords in
`.env` authoritative.

> **This section remains authoritative for full recovery.**
> `deploy/scripts/restore.sh` (added with the deploy-rollback work) automates
> steps 3 and 4 only and asserts the end state — the RLS policy count against
> the count the backup recorded, and that `app_user` is narrow. It restores
> **no key material**, so it is not a substitute for steps 1 and 5. Use it
> in place of steps 3-4 here, or on its own for a rollback on a live host
> where `openbao-data` and the CA were never lost.
>
> It differs from step 3 in one deliberate way: this section uses
> `pg_restore --clean --if-exists` into the existing database, while the
> script refuses a populated target unless `ITS_RESTORE_FORCE=1` and then
> drops the database. Both re-apply the narrowing afterwards, for the reason
> given below — which is the same reason in both cases.

```sh
cd deploy
docker compose down -v            # destroys ALL volumes — you are recovering
cp "$BK/env" .env                 # or recreate it; passwords must match the dump era

# 1. Key material first — restore before anything reads it.
for v in openbao-data its-ca its-secrets its-gateway-tls minio-data; do
  docker volume create "it-suite_$v" >/dev/null
  docker run --rm -v "it-suite_$v:/data" -v "$BK:/backup:ro" alpine \
    tar xzf "/backup/$v.tgz" -C /data
done

# 2. Fresh Postgres. The entrypoint runs init-app-user.sh, which creates
#    app_user/portal_user with the .env passwords and installs the ALTER
#    DEFAULT PRIVILEGES baseline. (Since own authentication phase 4 it no
#    longer creates a keycloak database.)
docker compose up -d --wait postgres

# 3. Restore the databases as its_admin.
docker compose exec -T postgres pg_restore -U its_admin -d itsuite  --clean --if-exists < "$BK/itsuite.dump"
# Only when the backup carries keycloak.dump (taken before own authentication
# phase 4, 2026-11-15, or before an upgrade with ITS_BACKUP_OLD_IDENTITY_DB=1):
[ -s "$BK/keycloak.dump" ] && {
  docker compose exec -T postgres createdb -U its_admin keycloak \
    && docker compose exec -T postgres pg_restore -U its_admin -d keycloak --clean --if-exists < "$BK/keycloak.dump"
}

# 4. RE-APPLY THE APP-ROLE PRIVILEGE NARROWING. This is not optional — see below.
docker compose run --rm postgres-grants

# 5. Unseal OpenBao with the operator-held keys, then bring the stack up.
docker compose up -d openbao
docker compose exec openbao bao operator unseal   # repeat to threshold
docker compose up -d
```

### Step 4 is not optional, and here is the proof

`pg_restore --clean` **drops and recreates every table**. Recreation happens as
`its_admin`, so the standing `ALTER DEFAULT PRIVILEGES` from
`init-app-user.sh` fires and grants `app_user` blanket
`SELECT, INSERT, UPDATE, DELETE`. `pg_dump` records ACLs as `GRANT` statements
and has no way to express "and revoke what the default privileges would have
added", so **the narrowings are silently lost**. Measured on a real restore of
this stack, immediately after step 3:

```
 devices:      SELECT,INSERT,UPDATE,DELETE
 permissions:  SELECT,INSERT,UPDATE,DELETE      <-- app_user can rewrite the RBAC catalog
 audit_events: SELECT,INSERT,UPDATE,DELETE      <-- app_user can rewrite the audit log
```

and after `docker compose run --rm postgres-grants`:

```
 devices:         SELECT,INSERT,UPDATE,DELETE
 permissions:     SELECT
 role_permissions:SELECT
 audit_events:    SELECT,INSERT
 security_events: SELECT,INSERT
```

A restore that skips step 4 comes up *working* and quietly violates
invariants #4 and #6. Nothing in the application will complain.

Row-level security itself survives the dump intact — `pg_dump` emits the
`ENABLE`/`FORCE ROW LEVEL SECURITY` and `CREATE POLICY` statements, and a
restored database measured 90 of 93 tables with RLS enabled *and* forced, the
same as before the dump (the three without are `alembic_version`,
`permissions`, and `role_permissions`, which are not tenant-scoped).

---

## 4. The audit chain

Every audit event's `hash` covers the previous event's `hash` plus the
canonical encoding of the event, and — when an HMAC key is configured, which
production requires — that link is an **HMAC-SHA256 keyed by
`ITS_AUDIT_HMAC_KEY_PATH`**, a secret the database role cannot read
(`backend/app/core/audit.py`).

### What a restore does to it

- **A complete restore with the same HMAC key: the chain verifies unchanged.**
  Nothing about the chain is positional or timestamp-derived; `pg_restore`
  reproduces `prev_hash`/`hash` byte for byte and verification is unaffected.
- **A restore without the key — or with a *different* key — verifies nothing,
  ever.** The links cannot be recomputed and cannot be repaired. There is no
  re-keying path and there should not be one: a chain an operator can rewrite
  is not evidence. **This is the single most important thing on the backup
  list** (table row 6). Back the key up separately from the database, and
  confirm it restores.
- **A point-in-time restore that loses the tail is fine.** The chain verifies
  over what survived; the missing events are missing, not corrupt. A restore
  that loses events from the *middle* — a partial or hand-edited dump — breaks
  every link after the gap, which is exactly the intended behaviour.
- **You cannot delete or edit rows to "fix" a chain.** `audit_events` carries a
  database trigger that rejects `UPDATE` and `DELETE` **for every role,
  including superusers**; a `DELETE` as `its_admin` fails with
  `ERROR: audit_events is append-only`. Only a table drop (as `pg_restore`
  does) removes rows.

### How an operator confirms the chain after a restore

**Authoritative check** — the keyed verification, for every organization. This
is the one that proves the HMAC key restored correctly. From `deploy/`, once
`api` is up, it runs the platform's own verification
(`audit_service.verify`, what `GET /api/v1/audit/verify` runs) inside the
`api` container, with that container's `.env` and HMAC key, and prints one
line per organization; it needs no sign-in and no token. ⚠ Not yet run inside
a running stack (the verification status at the top of this document).

```sh
docker compose exec -T api python - <<'EOF'
import asyncio
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.audit import audit_service, load_audit_hmac_key
from app.core.config import get_settings
from app.models import Organization


async def main() -> None:
    settings = get_settings()
    audit_service.set_hmac_key(load_audit_hmac_key(settings))
    engine = create_async_engine(settings.database_admin_url)
    try:
        async with AsyncSession(engine) as session:
            orgs = (await session.execute(select(Organization.id, Organization.slug))).all()
            for org_id, slug in orgs:
                r = await audit_service.verify(session, org_id)
                broken = str(r.broken_at) if r.broken_at else None
                print(slug, json.dumps({"ok": r.ok, "count": r.count, "broken_at": broken}))
    finally:
        await engine.dispose()


asyncio.run(main())
EOF
```

```
novieri {"ok": true, "count": 12345, "broken_at": null}
```

`ok: false` with a `broken_at` id names the first event that failed. **If `ok`
is false immediately after a restore and the row count looks right, suspect the
HMAC key before you suspect the data.** The console shows the same check for
the signed-in person's organization: **Audit log**, then **Verify chain** (it
needs `audit.read`). A `curl` with `Authorization: Bearer` is not a way to run
it: the console's API reads a session cookie, not a bearer token, since own
authentication phase 1.

**Pre-flight check** — a pure-SQL structural check you can run during the
restore, before the API is up and without any token. It validates the linkage
(nothing deleted, nothing reordered) but *not* the HMAC, so it is a fast
falsifier, not a substitute:

```sql
WITH ordered AS (
  SELECT id, org_id, prev_hash,
         lag(hash) OVER (PARTITION BY org_id ORDER BY id) AS expected_prev
  FROM audit_events)
SELECT org_id, id AS first_broken_link
FROM ordered
WHERE prev_hash IS DISTINCT FROM coalesce(expected_prev, repeat('0', 64))
ORDER BY id;
```

Zero rows means the linkage is intact. `ORDER BY id` is the correct ordering:
`audit_events.id` is a UUIDv7, so it is time-ordered, and it is the same
ordering `audit_service.verify()` uses. (Verified: an intact chain returns no
rows; introducing one mislinked event returns exactly that event.)

### Known caveat: worker-written events from before the key fix are permanently unverifiable

**Fixed going forward.** `create_celery()` now loads the HMAC key the same way
`app/main.py` and `app/gateway/main.py` do, so every audit event a Celery sweep
writes is keyed. Any worker process built from a release containing that fix is
correct.

**Not fixable retroactively.** Between the introduction of HMAC keying and that
fix, `app/worker.py` called `audit_service.record()` without ever calling
`set_hmac_key()`, so worker-written events (patch/deploy/backup dispatch,
provisioning timeouts, notification delivery, SLA breach escalation and
auto-close) landed as plain SHA-256 links inside an otherwise HMAC-keyed chain.
A deployment that ran the worker with `ITS_AUDIT_HMAC_KEY_PATH` set during that
window holds such rows today. There is no repair: `audit_events` is append-only
and the trigger rejects `UPDATE`/`DELETE` **for every role, including
superusers**, and re-keying a chain in place is exactly the operator-rewritable
history the design refuses to allow (§4 above). Rows written after the fix are
keyed correctly, but they do not heal the ones before it.

Operator-facing consequence, for an affected org:

- `/api/v1/audit/verify` returns `ok: false` with `broken_at` naming the
  **earliest** unkeyed worker-written event, and it will keep doing so forever.
  `verify()` stops at the first mismatch, so events *after* that id are not
  checked at all — the result proves only that everything *before* `broken_at`
  is intact.
- **This is not a failed restore.** Before concluding a restore is bad, look up
  `broken_at` in `/api/v1/audit`: if it names a system-attributed,
  worker-originated action (`actor_type: "system"`, e.g. `patch.scan_dispatched`,
  `provisioning.timed_out`, an SLA breach action) predating the fix, this caveat
  is the explanation and the restore is fine. A genuine key mismatch breaks at
  the *first* event in the chain, not partway through.
- Record the affected `broken_at` id per org as a known baseline so post-restore
  verification is compared against it rather than against `ok: true`. The
  tamper-evidence loss is bounded to those specific pre-fix worker rows: they
  are chained (a deletion or reordering still shows up in the SQL pre-flight
  above) but not authenticated, so they could have been forged or rewritten by
  anyone with database write access without detection. Treat them as
  corroborating records, not as evidence.
- Only a table drop — i.e. restoring the database from a dump taken before the
  affected rows existed — removes them, and that discards every later event too.
  That trade is almost never worth making; prefer the recorded baseline.

---

## 5. The internal CA and signing key

With the production settings (`ITS_CA_BACKEND=openbao`,
`ITS_SIGNING_BACKEND=openbao`, `ITS_VAULT_BACKEND=openbao`), **three
irreplaceable things live inside OpenBao's file storage**
(`openbao-data`, provisioned by `deploy/openbao/init.sh`):

1. **The `pki` mount** — the internal root CA (`common_name="it-suite internal
   CA"`, 10-year TTL) that signs every agent client certificate.
2. **The `transit/keys/job-signing` Ed25519 key** — the private half never
   leaves the vault and **cannot be exported**. It is not backup-able as a
   file; the only copy is inside `openbao-data`.
3. **The `transit` keys that wrap every org's vault DEK** — the credential
   vault stores `wrapped_dek` in Postgres and unwraps it through OpenBao. The
   database ciphertext is worthless without these.

### What to back up

- `openbao-data` (the volume — §2.2), **and**
- the **unseal keys and root token** from `bao operator init`, held outside
  the stack (§2.3), **and**
- `its-ca` (the exported root certificate the gateway trusts as its client CA;
  with `ITS_CA_BACKEND=local` this volume also holds `root.key`, the CA private
  key itself — back it up as key material, not as config).

The unseal keys are not optional. OpenBao's file storage is encrypted at rest
by keys derived from them; `openbao-data` without them restores nothing.

### Recovery path if the CA is lost

There is no partial recovery. If `openbao-data` or the unseal keys are gone:

1. **Every enrolled agent must re-enroll.** Their client certificates were
   issued by a CA that no longer exists, so mTLS admission at the gateway fails
   for the entire fleet. Re-run `deploy/openbao/init.sh` to mint a new root and
   a new signing key, distribute fresh enrollment tokens, and re-enroll every
   endpoint. Plan for it as a fleet-wide touch, not a maintenance window.
2. **Every stored credential is permanently unreadable.** Remote-control admin
   credentials, backup repository keys, license keys, provisioning answer-file
   secrets, notification channel secrets, and IMAP mailbox passwords are all
   envelope-encrypted under a DEK wrapped by the lost transit key. The
   ciphertext in Postgres survives the database restore and decrypts to
   nothing. They must be re-entered by hand, per org.
3. **In-flight signed jobs become invalid.** This is the mild one: jobs are
   short-lived (`ITS_JOB_DEFAULT_TTL_S`, 1h by default) and the expiry sweep
   clears them.

Point 1 is why the CA is called out separately from "backups" in general: it
is the only item on the list whose loss has a recovery cost measured in
endpoint visits.

> The OpenBao backup and restore commands in §2.2/§3 were **not** exercised in
> the environment this runbook was written in. Rehearse them (§6) before you
> need them.

---

## 6. Verifying a restore

Do this on a scratch host, on a schedule — a backup nobody has restored is a
hypothesis. The BCP module (`docs/modules/bcp.md`) is where the result of this
drill is recorded as SOC 2 evidence.

**Automated monthly, for the database half:** `deploy/scripts/restore-drill.sh`
(timer `its-restore-drill.timer`, `deploy/README.md` "Platform backups")
restores the latest nightly backup into the throwaway project
`itms-restore-drill` on the same host and runs steps 2, 4 (the role and
narrowing parts) and 6 (the SQL pre-flight) below, plus, for a backup that
holds `keycloak.dump` (taken before own authentication phase 4, or by
`deploy.sh` before an upgrade), a check that the restored `keycloak`
database holds tables. Steps 1, 3, 5, 7 and 8 need the key material and a full stack;
they remain a manual rehearsal on a scratch host.

1. **Stack comes up.** `docker compose ps` — every service `Up`, `postgres`
   and `minio` healthy, `migrate` and `postgres-grants` `Exited (0)`.
2. **Schema is at head.**
   `docker compose exec -T postgres psql -U its_admin -d itsuite -tAc "select version_num from alembic_version"`
   — matches the version the dump was taken at.
3. **Row counts match the source.** Spot-check `organizations`, `devices`,
   `ticket`, `audit_events`.
4. **The app role is still RLS-constrained.** This catches the step-4 regression
   above and any accidental privilege drift:
   ```sql
   SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'app_user';
   -- expect f | f
   SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner='app_user';
   -- expect 0 (an owner bypasses RLS unless FORCE is set)
   SELECT has_table_privilege('app_user','audit_events','UPDATE')
       OR has_table_privilege('app_user','permissions','INSERT') AS narrowing_lost;
   -- expect f
   ```
   The same assertions run in CI on every push (`.github/workflows/ci.yml`,
   job `deploy`).
5. **Tenant isolation holds.** Connect as `app_user` with no
   `app.current_org` set — every tenant table must return zero rows; pin the
   GUC to one org and only that org's rows appear. (`SET row_security = off`
   as `app_user` fails with `query would be affected by row-level security
   policy` — it cannot opt out.)
6. **The audit chain verifies.** §4 — both the SQL pre-flight and the keyed
   verification (the in-container check, or the console's **Verify chain**;
   both run what `/api/v1/audit/verify` runs).
7. **An agent still connects.** The real end-to-end proof that the CA restored:
   an existing enrolled endpoint reconnects over mTLS and a `hello` job
   round-trips, with no re-enrollment.
8. **A credential still decrypts.** Check out one stored credential through the
   API — this proves the OpenBao transit keys survived, which no database check
   can tell you.

Steps 7 and 8 are the ones people skip. They are the only two that test the
half of the system that lives outside PostgreSQL.

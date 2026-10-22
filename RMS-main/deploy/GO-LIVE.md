# Go-live checklist

Everything below must be **changed from the default** before this platform
faces anything real. It is domain-admin-equivalent command-and-control over an
entire fleet (CLAUDE.md); a default left in place here is not a rough edge, it
is the whole compromise.

Work top to bottom. Each item names the exact file or variable.

---

## 1. Secrets in `deploy/.env`

`cp .env.example .env` leaves **seven** values that must be replaced, and
**all seven are literally `change-me`** — verified against `.env.example`
2026-08-16, which had said "five" here. The distinction that matters is not
which ones say `change-me`; it is *when* you can replace them: five are
generated before you start anything, and the two OpenBao tokens do not exist
until first boot has run (§3.2, §3.4), so they stay `change-me` for a few
minutes by necessity.

`./scripts/preflight.sh` checks all of this for you and refuses on anything
left at a default. Run it before `docker compose up`, not after.

| Variable | Generate with | Notes |
|---|---|---|
| `POSTGRES_ADMIN_PASSWORD` | `openssl rand -base64 24` | `its_admin` — BYPASSRLS, the migration/bootstrap role |
| `APP_DB_PASSWORD` | `openssl rand -base64 24` | `app_user` — the RLS-constrained role every request runs as |
| `PORTAL_DB_PASSWORD` | `openssl rand -base64 24` | `portal_user` — the row-scoped end-user portal role |
| `KEYCLOAK_ADMIN_PASSWORD` | `openssl rand -base64 24` | the identity provider's bootstrap admin. **Used once, then rotated away by the init job** — see §2 |
| `MINIO_ROOT_PASSWORD` | `openssl rand -base64 24` | also used as `ITS_S3_SECRET_KEY`, which signs every presigned URL the fleet is handed — see §3 |
| `ITS_OPENBAO_TOKEN` | first boot step 3.4 | the **least-privilege `its-app`** token, never the root token |
| `OPENBAO_TOKEN` | first boot step 3.2 | the **provisioning/root** token; blank it out again after first boot |

**And one value that is not a secret but is not optional either:**

| Variable | Set to | Notes |
|---|---|---|
| `IT_SUITE_VERSION` | the short commit SHA, via `./scripts/build-images.sh` | `.env.example` ships `dev`. `:dev` names whatever was last built on the host, so it is not a deployable state and not a rollback target — a deployment left on it has nothing to roll back TO. Run `./scripts/build-images.sh` and set the line it prints. |

Then:

```sh
chmod 600 deploy/.env
```

`deploy/.env` is gitignored. Confirm before your first commit on the host:

```sh
git -C /opt/it-suite status --short deploy/.env    # must print nothing
```

**Do not put the two OpenBao tokens in the same place.** `ITS_OPENBAO_TOKEN` is
read by every long-running backend container (`backup-replicator` included); `OPENBAO_TOKEN` is read by one
short-lived provisioning container and can be emptied once `openbao-init` has
run. Leaving the root token in `.env` permanently gives every application
process the ability to delete the job-signing key.

## 2. Identity

Three instructions that used to be yours are now the stack's, and it is worth
knowing that before you go looking for them: rotating the bootstrap admin,
restricting the admin console, and creating the first administrator.

**The bootstrap admin retires itself.** `KEYCLOAK_ADMIN_USER` /
`KEYCLOAK_ADMIN_PASSWORD` still create a permanent `master`-realm account on
first boot — but the `keycloak-init` job now uses it once, to create the two
service clients the platform needs, and then **rotates its password to a random
value written only to the `its-secrets` volume**, mode 600, at
`/var/lib/its/secrets/keycloak-bootstrap-admin.secret`.

This supersedes the instruction that used to sit here asking you to create a
named admin and then delete the bootstrap account. Nothing needs deleting: the
account exists, no human knows its password, and no step in this runbook or in
`deploy/README.md` uses it. That is stronger than a request to please remove it,
because a request is only as good as the person reading it at 2am.

Two consequences to plan for rather than discover:

- **`KEYCLOAK_ADMIN_PASSWORD` in `.env` is stale after the first
  `docker compose up`,** and that is correct, not a fault. It stays in `.env`
  because the init job falls back to it if the volume's copy is missing or was
  never applied. Generate it properly anyway (§1) — it is live until the first
  run finishes.
- **The bootstrap password now exists in exactly one place.** `its-secrets` is
  already in the backup set for the audit HMAC key (§6); it is now also the
  only copy of the vendor's break-glass credential. Lose that volume *and*
  `.env` together and nobody — including the vendor — can authenticate as the
  master account again.

**There is no admin portal on `:443`, and nothing in normal operation needs
one.** `/admin` is not routed (`docker-compose.yml`, the `keycloak` labels);
Traefik forwards only what sign-in needs. Adding it back is a security
regression, not a convenience: that console can create any user and reset any
password in the fleet, behind one password, on the product's own hostname.
Realm-level work — federating AD/Entra, inspecting a login flow — is
**vendor-only** and goes over an SSH tunnel to the container, documented in
`deploy/README.md`, "Break-glass: the identity admin console".

**First boot is a screen, not a runbook.** `deploy/README.md`'s old steps 8–10
— create the first administrator by piping Python into `docker compose exec`,
insert the organization row by SQL, grant the role by a second SQL statement —
are replaced by:

0. **Configure email first if you can.** The setup screen finishes by *emailing*
   the administrator it just created, so an installation with no working SMTP
   finishes it with a detour. `deploy/README.md`, "Keycloak SMTP", is
   ten minutes and removes the whole of "if the invitation could not be sent"
   below. Skipping it is survivable, not free.

1. Open `https://<your host>/`. On an unclaimed installation it shows the
   setup screen — and that first request is what **generates** the install
   code, so do this before looking for it. Nothing runs earlier.

2. Read the code. The api logs it exactly once, at the moment it is created,
   and writes it to `/var/lib/its/secrets/setup-code` mode 600:

   ```sh
   docker compose logs api | grep -i "install code"
   ```

3. Enter it on that screen, with your name, email and organization name.

That creates the organization, the administrator account and its `org-admin`
grant in **one transaction**, through the same code paths the Users tab uses,
and writes all of it to the hash-chained audit log. Either all three exist or
none do.

**It does not sign you in, and that is deliberate rather than unfinished.**
ITMS issues no session and holds no password — the account it just created has
no credential at all yet. What the screen does instead is have the identity
provider send an **enrolment email**: choose a password, enrol a passkey, then
sign in the ordinary way. So the last screen of setup says one of two things,
and on an installation where you skipped step 0 it says the second:

| The screen says | What it means | What you do |
|---|---|---|
| "Check that inbox for a link to choose a password, then sign in." | the mail was accepted for delivery | open it, set a password, enrol the passkey, sign in. Note "accepted", not "arrived" — a relay can accept and still drop it |
| "The invitation email could not be sent…" | nothing is configured to send mail yet | the account, the organization and the grant all exist and are fine; it simply has no way to reach you. Read on |

**If the invitation could not be sent.** This is the expected first outcome on
an installation with no SMTP, not a failure of the claim — and **re-running
setup will not help**: the installation is claimed, the screen is gone, and the
claim route now answers "this installation has already been set up". Recovery is
vendor-side, through the SSH tunnel in `deploy/README.md`, "Break-glass: the
identity admin console", and there are two ways:

- **Fix the cause, then re-send** — the one to prefer. Set the realm's SMTP
  settings (`deploy/README.md`, "Keycloak SMTP"), then in the same
  console: **Users →** the administrator **→ Credentials → Credential Reset**,
  actions `Update Password` and `webauthn-register-passwordless`, **Send email**.
  That is the identical mail the setup screen tried to send.
- **Get in first, fix it from inside** — if mail is going to take a while. Same
  console: **Users →** the administrator **→ Credentials → Set password**,
  **Temporary: On**. Sign in with it, and configure delivery in ITMS itself at
  **Settings → Email** so that no later account ever needs this tunnel. Sign in
  promptly: that password is in your clipboard and in nobody's audit log.

Either way, check afterwards that the account really is an administrator —
**Settings → Users** should list it with the **org-admin** role. If it does not,
the claim did not complete and nothing above will substitute for it.

**The install code is the only thing between a stranger and this box.** An
appliance is reachable long before its owner sits down at it, and the setup
screen has to answer before any user exists, so it cannot be gated by a
permission. Zero-users alone is not a guard. Claim the installation from a
browser you trust, promptly, and treat the code as a password until you have.

Still yours to check, and none of it is automated:

- **MFA is a default required action** (`CONFIGURE_TOTP` in the realm import)
  but only for accounts created after the import. Verify every account has it.
- **If you enabled `directAccessGrantsEnabled` on the `it-suite-web` client**
  to script a first-boot verification, **turn it back off.** It is off in the
  committed realm import, and off on both service clients the init job creates.
  A client with the password grant enabled turns every leaked password into an
  API token with no browser and no MFA prompt.
- **Federate AD/Entra rather than creating local users**, and make sure the
  identity provider mapper populates the **`org`** attribute — it is declared
  in the realm's user profile (`components` →
  `org.keycloak.userprofile.UserProfileProvider`), and Keycloak **silently
  discards** any attribute not declared there. This is vendor-side work through
  the tunnel above.
- **Verify sign-in in a private window before you trust it.** The console has
  no launcher page of its own: an unauthenticated visitor is redirected
  straight to the identity provider (`docs/modules/console-auth/spec.md` §3.4).
  So a broken login theme, browser flow or identity provider leaves **no other
  way in**, and the failure looks like a redirect you cannot stop rather than
  an error. Change one of those three at a time, and check the result before
  changing the next — `deploy/README.md`, "Branding and extending sign-in", is
  in that order for this reason.
- **`keycloak-init` must have exited 0.** Nothing gates on it deliberately — a
  failure there should not stop the product from starting — so it is on you to
  look:

  ```sh
  docker compose ps -a keycloak-init      # Exited (0)
  docker compose logs keycloak-init       # says what it created; never a secret
  ```

  A non-zero exit means the two service clients may not exist, and the setup
  screen will tell you it cannot create an account. Re-run it — it is
  idempotent and re-running is its normal case:

  ```sh
  docker compose up keycloak-init
  ```

## 3. TLS

Two entirely separate certificate systems. Do not mix them.

**`:443`, operator-facing — your real public certificate.**

```sh
cp /etc/letsencrypt/live/itms.toeshee.com/fullchain.pem deploy/certs/fullchain.pem
cp /etc/letsencrypt/live/itms.toeshee.com/privkey.pem   deploy/certs/privkey.pem
chmod 600 deploy/certs/privkey.pem
docker compose restart traefik
```

`deploy/certs/` is a gitignored bind mount (`.gitkeep` keeps the directory).
Traefik **silently falls back to a self-signed certificate** if either file is
missing or unreadable — it does not fail. Always verify:

```sh
echo | openssl s_client -connect itms.toeshee.com:443 -servername itms.toeshee.com 2>/dev/null \
  | openssl x509 -noout -issuer -subject -dates
```

`issuer=CN = TRAEFIK DEFAULT CERT` means it fell back and you are serving a
self-signed certificate to your operators.

Renewal is yours to automate (`certbot renew --deploy-hook` that copies the two
files and runs `docker compose restart traefik`).

**`:8443`, agent-facing — the internal CA, never your public certificate.**
The gateway terminates mutual TLS itself (Traefik is TCP passthrough), and
agents pin the internal CA at enrollment. A publicly-issued certificate here
breaks every agent. See §5.

**`:443` also carries object storage, and endpoints must reach it.** The
`minio` router serves `/<bucket>/` on the same hostname and the same
certificate — deliberately, so no second DNS name or SAN is needed. Endpoints
fetch deployment artifacts and push backup data through it with presigned URLs
minted by the control plane; **it is not an alternative operator interface and
nothing on it is anonymously readable.** Before go-live, confirm all four:

```sh
# 1. The object route is live and refuses unsigned reads.
#    Expect: 403, and an <Error><Code>AccessDenied</Code> body from MinIO.
#    A 404 or your SPA's HTML means the route is MISSING and every artifact
#    download in the fleet is broken. A 200 is an emergency — see below.
curl -sk https://itms.toeshee.com/it-suite/probe -w '\n%{http_code}\n'

# 2. Listing does not reach MinIO. Expect: 0.
#    NOT a status check: everything the object route does not claim falls
#    through to the frontend's catch-all, so this returns 200 and index.html.
#    What must never appear is a bucket listing.
curl -sk 'https://itms.toeshee.com/it-suite?list-type=2' | grep -c '<ListBucketResult'

# 3. The MinIO console is not reachable. Expect: your SPA — never a MinIO
#    login page, and never a <Error> body.
curl -sk https://itms.toeshee.com/ | head -c 200

# 4. MinIO publishes no port of its own.
docker compose ps minio        # PORTS column must be empty
```

A `200` with your object's bytes on (1) means someone has made the bucket
publicly readable (`mc anonymous set download`). Undo it: this platform never
reads anonymously, so a public-read policy grants it nothing and grants
everyone else every artifact, every backup chunk and every session recording in
the fleet.

`ITS_S3_SECRET_KEY` (= `MINIO_ROOT_PASSWORD`) signs every presigned URL, so it
is a fleet-wide credential rather than a storage detail. It lives in `.env`,
not in OpenBao — treat it like the database passwords in §1, and rotate it if
`.env` is ever exposed. With external object storage, use an IAM identity
scoped to the one bucket instead of an account root key.

## 4. OpenBao seal

- **Store the 5 unseal key shares and the root token OFF this host**, split
  across different custodians. `bao operator init` prints them **exactly
  once** — losing threshold-many shares means the job-signing key, the agent
  CA and every sealed credential are unrecoverable.
- **OpenBao comes back SEALED after every restart and every host reboot**, and
  nothing starts until it is unsealed by hand:

  ```sh
  docker compose exec openbao bao operator unseal   # ×3, one share each
  ```

  The `openbao` healthcheck reports unhealthy while sealed and `openbao-init`
  waits on it, so a bare `docker compose up -d` blocks. `./scripts/deploy.sh`
  checks first and stops with these instructions instead.

- **Optional: auto-unseal with a static key** (`./scripts/openbao-seal.sh`,
  OFF by default). Decide it deliberately; the trade is real:

  | | Shamir (default) | Static key (auto-unseal) |
  |---|---|---|
  | After a reboot or power cut | job signing, the CA and the vault are **dead** until a person enters 3 of 5 shares | back on its own in seconds |
  | Someone copies the disk or an `openbao-data` backup | useless without the shares | useless **unless they also get the key file** |
  | Someone gets the key file **and** `openbao-data` | — | every vault key, the job-signing key and the CA: domain-admin over the fleet |
  | The key file is lost | — | **OpenBao cannot be unsealed at all.** The shares become recovery keys and cannot unseal |

  So, with auto-unseal: the key file lives on a separate path or mount,
  outside the repository, outside Docker's data root and outside the backup
  root (the script refuses those), mode `0400`; it is backed up **off-host,
  apart from any copy of `openbao-data`**; and the 5 shares are still kept —
  they authorize `disable` and `bao operator generate-root`. For one operator
  on one host, a platform that comes back after a power cut is usually worth
  more than protection against someone who already has root on the host,
  which is who can read that file. A TPM- or KMS-backed seal would remove the
  file; neither is wired up here.

  ```sh
  cd /opt/it-suite/deploy
  sudo ./scripts/openbao-seal.sh status
  sudo ./scripts/openbao-seal.sh enable    # asks for 3 shares (hidden), restarts OpenBao, proves it
  sudo ./scripts/openbao-seal.sh disable   # back to Shamir, with the same shares
  ```

  `enable` refuses unless OpenBao is running, unsealed and Shamir-sealed,
  snapshots `openbao-data` to `/var/backups/it-suite/openbao-seal/` first,
  writes the key to `/etc/it-suite/openbao-unseal/unseal.key` (or
  `--key-file`), adds `COMPOSE_FILE` and `OPENBAO_UNSEAL_KEY_FILE` to `.env`,
  and finishes by restarting OpenBao and checking it unsealed with no shares.
- **The host must not swap, or must swap to an encrypted device.** OpenBao 2.x
  dropped `mlock` entirely, so the process cannot pin its own memory.
- **Rotate `ITS_OPENBAO_TOKEN` on a schedule.** It is created with a 10-year
  TTL because nothing in the stack renews it; that is a deliberate trade, not
  an endorsement. Re-issue with
  `bao token create -orphan -policy=its-app -ttl=87600h`, update `.env`, and
  `docker compose up -d api gateway worker`.

## 5. Gateway certificate (the internal CA side)

Issued by `deploy/scripts/issue-gateway-cert.py` for **365 days**, and **it
does not renew itself**. Agent certificates rotate automatically at two-thirds
of life; this one has no such machinery, so put it on a timer:

```sh
sudo tee /etc/systemd/system/its-gateway-cert.service >/dev/null <<'EOF'
[Unit]
Description=Renew the it-suite gateway server certificate
[Service]
Type=oneshot
WorkingDirectory=/opt/it-suite/deploy
# The token arrives on STDIN from a 0600 file, never as an argument. The old
# form was  -e BAO_TOKEN="$(grep ^OPENBAO_TOKEN= .env ...)"  [removed],
# which put the ROOT token into the argv of `docker compose` -- world-readable
# via /proc/<pid>/cmdline, i.e. `ps`, for as long as the container ran. `-T` is
# what keeps stdin available to redirect.
ExecStart=/bin/bash -lc 'docker compose run --rm --no-deps -T \
  -v "$PWD/scripts/issue-gateway-cert.py:/tmp/issue-gateway-cert.py:ro" \
  --entrypoint python gateway /tmp/issue-gateway-cert.py itms.toeshee.com \
  < /etc/it-suite/gateway-issuer.token \
  && docker compose restart gateway'
EOF
sudo tee /etc/systemd/system/its-gateway-cert.timer >/dev/null <<'EOF'
[Unit]
Description=Monthly it-suite gateway certificate renewal
[Timer]
OnCalendar=monthly
Persistent=true
[Install]
WantedBy=timers.target
EOF
sudo systemctl enable --now its-gateway-cert.timer
```

This needs `/etc/it-suite/gateway-issuer.token` (mode 0600, root-owned) from
first-boot step 3.4. It does **not** need `OPENBAO_TOKEN`, and that is the
change worth noticing: the timer used to require the **root** token to stay on
the host, which §1 asks you to blank out — so the runbook was asking you to
choose between an unrenewed certificate and a permanently resident root
credential. The `gateway-issuer` policy holds `update` on `pki/issue/gateway`
and nothing else, so the file this timer reads cannot re-key
`transit/job-signing`, cannot re-mount the PKI, and cannot read any org's
envelope key. Blank `OPENBAO_TOKEN` as §1 says.

Rotate the `gateway-issuer` token on the same schedule as `ITS_OPENBAO_TOKEN`
above; both are minted with a 10-year TTL and nothing in the stack renews
either.

Check what is actually being served:

```sh
echo | openssl s_client -connect itms.toeshee.com:8443 2>/dev/null \
  | openssl x509 -noout -subject -dates
```

## 6. Back these up before you have anything to lose

Per `docs/07-backup-restore.md`, and all of it before the first real device
enrolls.

**First, schedule the database and object backups** (nightly, retention,
optional off-host copy, monthly restore drill — `deploy/README.md`, "Platform
backups"):

```sh
cd /opt/it-suite/deploy
sudo ./scripts/install-backup-timer.sh
sudo systemctl start its-platform-backup.service
```

Set `OFFSITE` in `/etc/it-suite/backup.conf` so a copy leaves this host. The
timers do **not** cover the key material below; those items stay manual and
go somewhere other than the database backups.

- **`its-secrets` volume — the audit HMAC key, and now more than that.**
  Restoring the database without it makes the entire audit chain permanently
  unverifiable, and there is no way to recompute it. Since the init job (§2) it
  also holds the **only** copy of the identity provider's bootstrap-admin
  password and the two service-client secrets. Back it up **off this host,
  immediately** after first boot.
- **`its-ca` volume — the client CA root** the gateway admits agents against.
- **The OpenBao unseal shares and root token** (§4). These are not on the host
  and must not be. **With auto-unseal, also the key file**
  (`/etc/it-suite/openbao-unseal/unseal.key`): off-host, and never beside a
  copy of `openbao-data`.
- **`openbao-data` volume** — the sealed vault: job-signing key, agent CA
  private key, every per-org envelope key.
- **`postgres-data`** — take logical dumps, not only volume snapshots:
  ```sh
  cd deploy && ./scripts/backup.sh ~/backups/$(date -u +%FT%H%MZ)
  ```
  That writes **three** artefacts, and the third is the one a hand-rolled
  `pg_dump` misses:
  - `itsuite.dump` — the application database.
  - `keycloak.dump` — your realm, users and federation config. Losing it
    locks every operator out even with an intact application database.
  - `globals.sql` — the cluster's **role definitions**, and therefore the
    roles' **SCRAM password hashes**. `docs/07-backup-restore.md` §2.1: *"It
    is a secret. Encrypt it, or skip it entirely."* `backup.sh` writes it
    mode 600 and now writes it unconditionally, so encrypt the backup
    directory before it leaves the host (§2.4). Note the tension with that
    advice: `restore.sh` requires the file, so "skip it entirely" means
    restoring by hand per §3 instead. A `pg_dump` of a
    single database emits zero `CREATE ROLE` statements while referencing
    `its_admin`, `app_user` and `portal_user` throughout ownership, grants
    and RLS policies. Restoring into a host this compose stack built is
    fine — `init-app-user.sh` has already created all three — but restoring
    into a bare cluster or a managed Postgres without `globals.sql` is
    `pg_restore` exit 1 with **266** `role ... does not exist` errors
    (measured: 130 `app_user`, 128 `its_admin`, 8 `portal_user`). The tables
    and their RLS policies still land, so the failure looks partial.

  `backup.sh` refuses to report success if any artefact is empty or if
  `globals.sql` is missing any of the three roles — an empty backup you
  trust is worse than no backup.
- **`minio-data`** — backup chunks, deployment artifacts, session recordings,
  ticket and invoice attachments. The nightly timer mirrors it (`OBJECTS` in
  `backup.conf`); switch to `no-fleet-backups` once the off-site copy in §8
  protects fleet data.
- **`deploy/.env` and `deploy/certs/`** — not in git by design, therefore not
  in any git-based recovery.

Verify a restore before go-live, not after an incident. **The authoritative
full-recovery procedure is `docs/07-backup-restore.md` §3** — key-material
volumes first, then a fresh Postgres, then the databases, then the narrowing,
then unseal OpenBao. `restore.sh` below covers only the database half of
that; a restored database with no `openbao-data` cannot sign a job or decrypt
a credential. Use it in place of §3's steps 3-4, or on its own for a rollback
on a live host where the key material was never lost:

```sh
./scripts/restore.sh ~/backups/<dir>          # into an EMPTY itsuite
ITS_RESTORE_FORCE=1 ./scripts/restore.sh ~/backups/<dir>   # REPLACES a populated one
```

The second form drops the existing `itsuite` database first. `restore.sh`
refuses a non-empty target unless you set that, because restoring over a
populated database leaves one that is neither the old nor the new — but a
real rollback always has a populated target, so that is the form the
rollback runbook uses.

It runs the ordered procedure — roles if absent, create database,
`pg_restore`, then `grant-app-privileges.sql` — and **verifies `app_user` is
narrow on the protected tables afterward**, exiting non-zero if it is not.
The narrowing step is required on **every** restore. On the primary path it
is a genuine repair (the restore widens `app_user`); after an
`ITS_RESTORE_FORCE` drop it is insurance rather than repair, because
dropping the database also destroys the standing rule that does the
widening. Run it either way. `init-app-user.sh`'s standing `ALTER DEFAULT PRIVILEGES` grants blanket
rights to every table `pg_restore` creates, and `pg_dump` cannot express the
compensating `REVOKE` — so without step 4 `app_user` comes back holding
`DELETE,INSERT,SELECT,UPDATE` on `audit_events`, `permissions`,
`role_permissions` and `security_events` (measured). `permissions` and
`role_permissions` have no trigger backstop, so that state lets the
application role rewrite the RBAC catalogue. This is what
`docs/07-backup-restore.md` §3 has said all along.

The `itsuite` restore has been exercised end to end between two clusters
shaped like this one. **The `keycloak` restore has not been verified beyond
running** — no realm, user or federation check has been done after one.

## 7. Deployment hygiene

- **`ITS_ENVIRONMENT=production`** (the default in `.env.example`). It makes
  the api and gateway fail closed on missing key material instead of silently
  generating a fresh signing key or CA.
- **`ITS_VAULT_BACKEND=openbao`.** Left unset it silently defaults to `local`
  — a root key on the container's own disk — which is a real downgrade of
  invariant #3, not a convenience.
- **`ITS_MULTI_TENANT`**: leave `false` for single-org on-prem. Set `true` only
  once every account reliably carries the `org` claim; in multi-tenant mode a
  token without it is rejected outright.
- **Pin every image.** `openbao`, `minio`, `traefik`, `postgres`, `keycloak`
  and `redis` (Valkey `8.1.10-alpine` since 2026-09-23) are pinned in
  `docker-compose.yml`. Keep it that way: an
  unattended `docker compose pull` that moves a secret store across a major
  version is how first-boot step 3 became impossible in the first place.
- **Firewall.** Publish only `443` and `8443`. Postgres, Redis, MinIO, OpenBao
  and Keycloak's HTTP port have no published ports and must stay that way.
- **`ITMS_BIND_IP`** — leave at `0.0.0.0` on a dedicated host. On a host that
  already runs something else on `:443`, set it to the address ITMS answers on;
  leaving it wildcard there does not degrade gracefully, it stops traefik from
  starting at all ("address already in use"), because Linux refuses a wildcard
  bind for a port any specific address already holds. `./scripts/host-check.sh`
  §2 prints the existing listener's bind address, which is the fact that decides
  whether a second address is even available. The address you pick must be the
  one your DNS name resolves to and the one your `:443` certificate covers.
- **Multi-homed host: read `host-network.md` §2 before touching routing.** If
  ITMS answers on a NIC that is not the default route's, replies leave via the
  wrong interface and get dropped as spoofed. It does not fail cleanly —
  handshakes succeed and sessions then stall, so it reads as an unreliable agent
  fleet and the gateway gets blamed while testing fine from every network you
  happen to test from. **Do not reach for a source-address policy rule**: that
  is the textbook fix for a service on the host, it cannot match the replies of
  a containerised one behind a published port, and it breaks the host's own
  access to that port. §2 shows the counter-example and the connection-marking
  alternative, and marks the latter unverified.
- **The clock must say `synchronized: yes` before first boot**, not after.
  Certificate validity and token expiry are absolute times, and `NTP service:
  active` only means the daemon is running — it will run happily having never
  reached a source. `host-network.md` §4.
- **Restart policies** are `unless-stopped` on every long-running service, so
  the stack returns after a host reboot — except OpenBao, which returns
  **sealed** (§4) unless you enabled auto-unseal.

## 8. Off-site backup copy and restoring with this server gone

Off-site copies (Backup → Off-site copy) need three things, in this order,
and the console refuses the next step until the previous one is done:

1. **A recovery key.** Make it on a computer that is not this server:

   ```
   pip install cryptography boto3 blake3
   python3 scripts/itms_recover.py keygen --out recovery.key
   ```

   Paste the printed `itms-recovery-public:` line into the console, confirm
   you stored `recovery.key` in two safe places, then run the `prove` command
   the console shows and paste its answer. This server never sees the
   private key. **If this server and `recovery.key` are both lost, the
   off-site copy cannot be read by anyone.**
2. **A bucket created with Object Lock**, and a key for it that can write,
   read and set retention but cannot change the bucket or bypass its lock.
   Add it in the console and press **Test**; the `backup-replicator`
   container runs the test within a minute. A bucket without Object Lock is
   refused.
3. **Turn it on per repository.** The `backup-replicator` container copies
   every complete backup point, oldest first; the first copy of a large
   server can take days over a WAN.

To restore when this server is gone, on any machine with the bucket key and
`recovery.key` (the capitalised words are yours to fill in: the bucket name
and address, then the repository and point ids that `list` prints):

```
export AWS_ACCESS_KEY_ID=...   AWS_SECRET_ACCESS_KEY=...
python3 scripts/itms_recover.py list --key recovery.key --bucket BUCKET --endpoint URL --prefix itms
python3 scripts/itms_recover.py restore --key recovery.key --bucket BUCKET --endpoint URL \
    --prefix itms --repository REPOSITORY_ID --point POINT_ID --dest ./restored
```

`list` prints repository and point ids with host names and times. The CI
job `backup-e2e` runs this restore against a second MinIO on every push.

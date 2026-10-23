# Deploying it-suite (on-prem, single node)

**Read [`GO-LIVE.md`](GO-LIVE.md) before this faces anything real.** It lists
every default that must be changed — secrets, TLS, the Keycloak admin account,
the OpenBao seal, the audit-chain key backup, database backups.

## Status of this runbook

Every command in the "First boot" section below **was executed**, top to
bottom, from completely empty volumes (`docker compose down -v`), against
Docker Engine 29.3.1 / Compose v5.1.1, and the output shown is real output.
The run finished with a device enrolled and a signed `hello` job round-tripped:
dispatch → mTLS delivery → verified execution on the endpoint → result →
hash-chained audit entry, with `GET /api/v1/audit/verify` reporting
`{"ok":true}`.

The previous version of this runbook had never been run. Executing it found
eleven defects that made it impossible to follow as written — OpenBao could not
start, could not be initialised, and could not issue the certificates the
gateway and the agents need; Traefik discovered no containers at all; no device
could enroll; and the first administrator was locked out of every endpoint.
They are fixed in this repository. "What was broken, and how you would
recognise it" at the end of this file lists them, because you will meet the
same symptoms if you ever diverge from these versions.

**What is NOT verified** is marked inline as ⚠ **UNVERIFIED** wherever it
appears. Nothing else in the first-boot section is a guess.

---

## Prerequisites (Ubuntu Server 24.04 / 26.04)

The deployment model is: clone this repository onto the host, run the stack
from `deploy/`, and `git pull` to upgrade.

### Docker Engine

⚠ **UNVERIFIED — these five commands were not run on a fresh Ubuntu host**
(this runbook was executed in a container that already had Docker 29.3.1).
They are Docker's own documented Ubuntu install, reproduced here because the
version requirement below is not optional.

**Do not use Ubuntu's `docker.io` apt package.** It ships an Engine old enough
that `traefik:v3.6`'s Docker provider is refused by it, or — the other
direction, which is what actually bit here — an Engine new enough to refuse
old Traefik. Either way, **Traefik silently discovers no containers and every
single route answers a bare 404** while every container looks healthy. Install
Docker's own repository:

```sh
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Then check the two versions that matter:

```sh
docker version --format '{{.Server.Version}}'   # need >= 26; verified on 29.3.1
docker compose version                          # need v2; verified on v5.1.1
```

Compose **v2.24 or newer** is required for a second reason: `.env` is used both
as Compose's project env file *and* as the services' `env_file`, and entries
like

```
ITS_DATABASE_URL=postgresql+asyncpg://app_user:${APP_DB_PASSWORD}@postgres:5432/itsuite
```

rely on Compose interpolating `${APP_DB_PASSWORD}` **inside** the env file.
Verified working on v5.1.1. On an older Compose the literal string
`${APP_DB_PASSWORD}` is passed to the container and every database connection
fails authentication.

### DNS, firewall, clone

- An `A` record for the host — this runbook uses **`itms.toeshee.com`**
  throughout. Substitute yours everywhere, including in `.env`
  (`ITS_OIDC_ISSUER`, `ITS_GATEWAY_URL`, `ITS_GATEWAY_RELAY_URL`) and in the
  gateway certificate (step 6).
- Inbound **443** and **8443**. Nothing else — every other service is internal
  to the compose network with no published port. Both ports must be reachable
  **from every managed endpoint**, not only from operators: `8443` is the agent
  gateway, and `443` carries object storage under presigned grants, which is how
  backup chunks and `app.deploy` artifacts travel. Assuming agents only need the
  gateway is failure #12 in the table at the end of this file.
- **On a shared or multi-homed host, read `host-network.md` before going
  further.** It covers the three things no container can fix: the bind address
  (a wildcard bind *fails* on a host where something else already holds 443),
  the reply path when ITMS is not on the default route's NIC (which presents as
  flaky agents, not as an outage), and the fact that ufw does not filter
  Docker-published ports at all.
- **If this host already runs another platform, read the bind address before
  anything else.** Run `scripts/host-check.sh` (it changes nothing) and look at
  section 2. A listener shown as `0.0.0.0:443` holds the port on every
  interface, so a second IP does not free it and one of the two platforms must
  move; a listener shown as `1.2.3.4:443` holds one address only, and ITMS can
  take another by setting `ITMS_BIND_IP` in `.env`. Linux refuses a wildcard
  bind for a port any specific address already holds, so leaving `ITMS_BIND_IP`
  at `0.0.0.0` on such a host makes traefik fail to start with "address already
  in use" — the whole stack, not just the shared port. Whatever address you
  pick must be the one your DNS name resolves to and the one your certificate
  is issued for.
- Clone. Over HTTPS this is one command; over SSH with a **deploy key** — a
  read-only key scoped to this repository, which is what a puller should have —
  it is:

  ```sh
  # On the server, as the user that will own the checkout.
  ssh-keygen -t ed25519 -f ~/.ssh/itms_deploy -C "itms-deploy@$(hostname)"
  cat ~/.ssh/itms_deploy.pub     # ONE line, starts `ssh-ed25519 AAAAC3Nza…`
  ```

  Paste that whole line into the repository's **Settings → Deploy keys → Add
  deploy key**, with "Allow write access" left **unchecked**. Two notes, both
  of which have cost real time: a terminal soft-wraps that ~100-character line,
  and copying the wrapped display inserts real newlines that GitHub rejects as
  "not in OpenSSH public key format" — as does pasting `~/.ssh/itms_deploy`,
  the private half, which begins `-----BEGIN`. `ssh-keygen -l -f
  ~/.ssh/itms_deploy.pub` prints a fingerprint if the file itself is sound,
  which distinguishes a bad file from a bad paste.

  Then pin the key to `github.com` so ssh does not offer every other identity
  it can find:

  ```sh
  cat >> ~/.ssh/config <<'EOF'
  Host github.com
    IdentityFile ~/.ssh/itms_deploy
    IdentitiesOnly yes
  EOF
  chmod 600 ~/.ssh/config
  ssh -T git@github.com      # "Hi …! You've successfully authenticated,
                             # but GitHub does not provide shell access."
                             # is SUCCESS, not an error.
  ```

  ```sh
  sudo mkdir -p /opt/it-suite && sudo chown "$USER" /opt/it-suite
  git clone git@github.com:Toeshee-Lab/Toeshee-RMS.git /opt/it-suite
  cd /opt/it-suite/deploy
  ```

  All commands below are run from `/opt/it-suite/deploy`.

---

## First boot

Start from genuinely empty state. If you are re-running after a failed attempt,
`docker compose down -v` first — a half-initialised Postgres data directory or
OpenBao barrier will hide the step that actually broke.

### Step 1 — `.env`

```sh
cp .env.example .env
chmod 600 .env
```

Set every `change-me`. Five you generate now; **two you cannot know yet** —
`ITS_OPENBAO_TOKEN` and `OPENBAO_TOKEN` come out of step 3 and are filled in at
step 3.5. Leave them as `change-me` for now: Compose only requires them to be
non-empty, so the literal `change-me` gets you through steps 2 and 3.

```sh
for v in POSTGRES_ADMIN_PASSWORD APP_DB_PASSWORD PORTAL_DB_PASSWORD \
         KEYCLOAK_ADMIN_PASSWORD MINIO_ROOT_PASSWORD; do
  sed -i "s|^$v=change-me$|$v=$(openssl rand -base64 24 | tr -d '=+/')|" .env
done
sed -i 's/your-host/itms.toeshee.com/g' .env
```

Then confirm what remains:

```sh
grep -c 'change-me' .env
```

```
2
```

Exactly two — the OpenBao pair. Real secrets never enter git; `.env` is
gitignored.

The `your-host` rewrite above also sets `ITS_S3_PUBLIC_ENDPOINT`, which is
where **endpoints** reach object storage and the only endpoint presigned URLs
are signed against. With the bundled MinIO that is this server's `:443`
hostname and there is nothing further to do; confirm it took:

```sh
grep '^ITS_S3_PUBLIC_ENDPOINT=' .env
```

```
ITS_S3_PUBLIC_ENDPOINT=https://itms.toeshee.com
```

If you are using object storage that is not the bundled MinIO, this is a
different host from `ITS_S3_ENDPOINT` and the rewrite has not given you the
right value — see the external-storage block in `.env.example`. Either way it
must not stay unset: with `ITS_ENVIRONMENT=production` the api and worker
refuse to touch object storage without it, rather than mint artifact URLs
naming `minio` that no endpoint can resolve.

### Step 2 — Postgres

```sh
docker compose up -d postgres
```

First boot runs `postgres/init-app-user.sh`, which creates `app_user` and
`portal_user` (both NOSUPERUSER, NOBYPASSRLS), grants `its_admin` BYPASSRLS
(the migration/bootstrap role), creates the `keycloak` database, and installs
`app_user`'s standing `ALTER DEFAULT PRIVILEGES` so any table a future
migration adds is usable with no manual step. No table exists yet, so `app_user`
cannot use anything yet either — **step 4b is not optional**.

```sh
docker compose logs postgres | grep -E 'CREATE ROLE|ALTER DEFAULT|CREATE DATABASE'
docker compose ps postgres --format '{{.Status}}'
```

```
postgres-1  | CREATE DATABASE
postgres-1  | CREATE ROLE
postgres-1  | CREATE ROLE
postgres-1  | ALTER DEFAULT PRIVILEGES
postgres-1  | ALTER DEFAULT PRIVILEGES
postgres-1  | CREATE DATABASE
Up 15 seconds (healthy)
```

**If it exits 1 with `APP_DB_PASSWORD: APP_DB_PASSWORD must be set`** you
skipped step 1. Fix `.env`, then `docker compose down -v postgres` — the init
script only ever runs against a brand-new data directory, so a partially
initialised volume must be destroyed, not restarted.

### Step 3 — OpenBao: initialise, unseal, provision

This is the step with the most moving parts, and every one of them is a place a
first boot can dead-end. Take it in order.

#### 3.1 Start it

```sh
docker compose up -d openbao
docker compose exec openbao bao status
```

```
Key                Value
---                -----
Seal Type          shamir
Initialized        false
```

`Initialized false` and a clean exit is correct. (The `bao` CLI defaults to
`https://`, but the listener is plaintext inside the compose network only;
`BAO_ADDR=http://127.0.0.1:8200` is set on the service so these commands work
as written. Without it every one of them fails with `http: server gave HTTP
response to HTTPS client`.)

#### 3.2 Initialise — this prints the only copy of the unseal keys

```sh
docker compose exec openbao bao operator init -key-shares=5 -key-threshold=3
```

**Five unseal key shares and one root token are printed exactly once and are
never recoverable.** Before you go further:

- copy the five shares to five different places — a password manager, sealed
  envelopes with different custodians, an offline vault. Threshold is 3, so
  three lost shares is a lost vault: the job-signing key, the agent CA and
  every sealed credential become unrecoverable, and every enrolled endpoint
  becomes uncontrollable.
- copy the root token to your password manager.
- **none of this goes into this repository, into `.env` (beyond the two token
  variables), into a shell script, or into a log.** This runbook deliberately
  shows no key material.

#### 3.3 Unseal — three of the five shares

```sh
docker compose exec openbao bao operator unseal   # prompts; repeat ×3
docker compose exec openbao bao status
```

```
Initialized     true
Sealed          false
Total Shares    5
Threshold       3
```

Then confirm the service reports healthy — the healthcheck is `bao status`, so
it goes healthy only when unsealed:

```sh
docker compose ps openbao --format '{{.Status}}'
```

```
Up 20 seconds (healthy)
```

> **This happens again every restart.** OpenBao returns **sealed** after any
> `docker compose up -d` that recreates it, and after every host reboot. The
> healthcheck plus `openbao-init`'s `service_healthy` dependency turn that into
> a clean wait rather than a confusing cascade, but `docker compose up -d` will
> block until you unseal. See GO-LIVE.md §4, which also describes the
> optional auto-unseal (`./scripts/openbao-seal.sh enable`) and what it costs.

#### 3.4 The runtime policies and their tokens

Nothing must **run** with the root token. Two least-privilege policies are
shipped and mounted into the container:

| policy | file | who uses it | what it may do |
|---|---|---|---|
| `its-app` | `/bao/its-app.hcl` | api, gateway, worker (`ITS_OPENBAO_TOKEN`) | sign jobs, seal/unseal org secrets, sign agent CSRs |
| `gateway-issuer` | `/bao/gateway-issuer.hcl` | `scripts/issue-gateway-cert.py` (step 6) | `update` on `pki/issue/gateway`, and nothing else |

> **Do not paste the root token onto a command line.** `docker compose exec -e
> BAO_TOKEN=<root-token> ...` puts it in the argv of `docker compose`, and
> `/proc/<pid>/cmdline` is world-readable — any user on the host can read it out
> of `ps` while the command runs, and your shell history keeps a copy
> afterwards. Open one shell inside the container and read the token into it
> with echo off instead. It never becomes an argument of anything.

```sh
docker compose exec openbao sh
```

then, inside that shell:

```sh
stty -echo; read -r BAO_TOKEN; stty echo; echo; export BAO_TOKEN   # paste the root token, Enter

bao policy write its-app /bao/its-app.hcl
bao policy write gateway-issuer /bao/gateway-issuer.hcl
bao auth tune -max-lease-ttl=87600h token/
bao token create -orphan -policy=its-app        -ttl=87600h
bao token create -orphan -policy=gateway-issuer -ttl=87600h
exit
```

```
Success! Uploaded policy: its-app
Success! Uploaded policy: gateway-issuer
Success! Tuned the auth method at: token/
```

and the two `token create` commands each print a token — the first with
`token_policies [default its-app]`, the second with
`token_policies [default gateway-issuer]` — both `token_duration 315360000s`.

The `auth tune` is required: without it the default 32-day system max TTL caps
the tokens, and the entire platform stops signing jobs a month after go-live.
The 10-year TTL is a deliberate trade — nothing in the stack renews these tokens
— so put a rotation reminder in GO-LIVE.md's schedule rather than trusting it.

`-orphan` matters: a child of the root token dies when the root token is
revoked.

Keep the `gateway-issuer` token where step 6 and the GO-LIVE.md §5 timer can
read it, and nowhere else:

```sh
sudo install -d -m 700 /etc/it-suite
sudo install -m 600 /dev/null /etc/it-suite/gateway-issuer.token
sudo tee /etc/it-suite/gateway-issuer.token >/dev/null   # paste, Ctrl-D
```

#### 3.5 Put both tokens in `.env`

```
ITS_OPENBAO_TOKEN=<the its-app token from 3.4>
OPENBAO_TOKEN=<the root token from 3.2>
```

They are **two different values** and must stay that way. `ITS_OPENBAO_TOKEN`
is what the api, gateway and worker use for every signature, every envelope
encryption and every certificate; `OPENBAO_TOKEN` is used only by the one-shot
`openbao-init` container, because enabling mounts needs privileges no runtime
policy has.

**The gateway certificate is no longer a third user of `OPENBAO_TOKEN`.** Step 6
used to expand it into a `docker compose run` command line; it now takes the
`gateway-issuer` token, on stdin or a prompt. `its-app.hcl` exists precisely so
the application does not hold root, and a monthly timer reading a token off the
host has the same claim on that reasoning: one capability on one path, instead
of the credential that can re-key `transit/job-signing`.

```sh
grep -c 'change-me' .env
```

```
0
```

#### 3.6 Provision the transit key and the PKI

```sh
docker compose up openbao-init
```

```
openbao-init-1  | openbao provisioning complete
openbao-init-1 exited with code 0
```

This creates the Ed25519 `job-signing` transit key, the `pki` mount with its
root CA, the `agent` role (client certificates, Ed25519, with the
`its://org/<org>/device/<device>` URI SAN the identity binding depends on) and
the `gateway` role (the gateway's own server certificate). The credential vault
(`ITS_VAULT_BACKEND=openbao`) reuses the same `transit` mount for its per-org
keys, created on first use — nothing extra to provision.

**If this exits 2 with `Vault is sealed`,** unseal (3.3) and re-run. It is
idempotent.

### Step 4 — Migrations, then table privileges

```sh
docker compose up migrate
```

```
migrate-1 exited with code 0
```

Alembic is quiet on success. Check the version directly:

```sh
docker compose exec -e PGPASSWORD=<POSTGRES_ADMIN_PASSWORD> postgres \
  psql -U its_admin -d itsuite -tAc 'select version_num from alembic_version;'
```

```
0079
```

That must equal the highest file in `backend/migrations/versions/`, and it is
the one number in this runbook that is stale the moment it is written. (The
first-boot run reached `0048`; the chain is at `0079` as of 2026-08-26 —
**check the tree you pulled** rather than trusting either number. `ls
backend/migrations/versions/ | sort | tail -1` is the authority.)

#### 4b — `postgres-grants`

```sh
docker compose up postgres-grants
```

```
postgres-grants-1  | GRANT
postgres-grants-1  | GRANT
postgres-grants-1  | REVOKE
postgres-grants-1  | REVOKE
postgres-grants-1  | REVOKE
postgres-grants-1 exited with code 0
```

This applies `app_user`'s real table privileges
(`postgres/grant-app-privileges.sql`): blanket read/write, narrowed so it
cannot rewrite the audit log or the RBAC catalog. Skipping it is the difference
between the app working and every request 500ing with
`permission denied for table ...` — see "The app_user table-privilege gap"
below for why it is two steps rather than one.

`api`, `gateway` and `worker` all declare `postgres-grants` completing
successfully as a hard dependency, so step 7 would run both of these anyway.
They are written out because you want to see them succeed on their own.

### Step 5 — The audit HMAC key

The api, gateway and worker load this at startup and, with
`ITS_ENVIRONMENT=production`, **refuse to start without it**
(`FileNotFoundError: /var/lib/its/secrets/audit-hmac.key`). It lives on the
shared `its-secrets` volume so all three read the *same bytes*; two different
keys silently produce two divergent chains for one org.

```sh
docker compose run --rm --no-deps --entrypoint sh api -c \
  'head -c 32 /dev/urandom > /var/lib/its/secrets/audit-hmac.key && \
   chmod 600 /var/lib/its/secrets/audit-hmac.key'

docker compose run --rm --no-deps --entrypoint sh api -c \
  'ls -l /var/lib/its/secrets/audit-hmac.key'
```

```
-rw------- 1 its its 32 Aug  5 21:57 /var/lib/its/secrets/audit-hmac.key
```

**Back it up off this host immediately** —
[`docs/07-backup-restore.md`](../docs/07-backup-restore.md). Restoring the
database without it leaves the entire audit chain unverifiable, and there is no
way to recompute it.

### Step 6 — The gateway's certificate, and the client CA

**Before** anything starts the gateway. The gateway loads its TLS material at
startup and exits immediately if it is absent:

```
ctx.load_cert_chain(certfile, keyfile)
FileNotFoundError: [Errno 2] No such file or directory
```

Issue it from the **internal** CA — not your public certificate; agents pin the
internal CA at enrollment and will reject anything else. Pass the exact
hostname agents dial in `ITS_GATEWAY_URL`. It prompts for the `gateway-issuer`
token from step 3.4, with echo off:

```sh
docker compose run --rm --no-deps \
  -v "$PWD/scripts/issue-gateway-cert.py:/tmp/issue-gateway-cert.py:ro" \
  --entrypoint python gateway /tmp/issue-gateway-cert.py itms.toeshee.com
```

```
OpenBao gateway-issuer token:
gateway.crt  /var/lib/its/tls/gateway.crt  (CN=itms.toeshee.com, 365d)
gateway.key  /var/lib/its/tls/gateway.key  (mode 0600)
root.crt     /var/lib/its/ca/root.crt  (agent client CA)
serial       57:d6:c9:f1:0a:85:72:29:6d:b0:3f:66:8a:1e:40:89:28:ee:a4:46
```

Or non-interactively, from the 0600 file step 3.4 wrote — note `-T`, which is
what leaves stdin available:

```sh
sudo cat /etc/it-suite/gateway-issuer.token | docker compose run --rm --no-deps -T \
  -v "$PWD/scripts/issue-gateway-cert.py:/tmp/issue-gateway-cert.py:ro" \
  --entrypoint python gateway /tmp/issue-gateway-cert.py itms.toeshee.com
```

**A pipe, not a `< redirect`, and the `sudo` covers `cat` alone.** This step ran
as `-bash: /etc/it-suite/gateway-issuer.token: Permission denied` on the first
real deploy. A `<` redirect is opened by the *shell*, so it runs as the operator
— and the token is deliberately `600 root:root` inside a `700` directory, which
the operator therefore cannot read. The redirect form only works for a root
shell, and this runbook's own prerequisites put you in an unprivileged one.

Do not "fix" it by running the whole line under `sudo`: `docker compose` would
then run as root, resolving a different `$PWD` and a different Docker context
than every other command here. Do not chown the token to the operator either —
GO-LIVE.md §5's renewal timer reads it as root, which is the reason for the
mode. Elevating only `cat` keeps the token off argv and out of shell history,
which is what the whole step is for.

**Neither form puts the token on a command line, and that is the point.** The
previous version of this step read (quoted, not `sh`-fenced — it is a line that
was removed, and a copy-pasteable bad example is a bad example you will meet
again):

```text
  -e BAO_TOKEN="$(grep '^OPENBAO_TOKEN=' .env | cut -d= -f2-)" \
```

which expands the **root** token into the argv of `docker compose`, readable by
any user on the host through `ps` for as long as the container runs, and left in
shell history afterwards — in the runbook for a script whose whole reason to
exist is that key material must not touch shell history. `BAO_TOKEN` is still
honoured when it is *already* in the environment (that is what the GO-LIVE.md §5
timer's `EnvironmentFile=` is for); nothing here constructs it.

It runs through the `gateway` service because that is the only container
mounting both `its-gateway-tls` and `its-ca`, so the private key is written
straight to the volume the gateway reads and never touches your shell history
or the host filesystem. `root.crt` is the CA the gateway admits agent client
certificates against (`ITS_GATEWAY_CLIENT_CA`).

**This certificate is 365 days and does not renew itself.** Install the timer
from GO-LIVE.md §5 now, while you are thinking about it.

### Step 7 — Your TLS certificate for `:443`, then start everything

```sh
cp /etc/letsencrypt/live/itms.toeshee.com/fullchain.pem certs/fullchain.pem
cp /etc/letsencrypt/live/itms.toeshee.com/privkey.pem   certs/privkey.pem
chmod 600 certs/privkey.pem
```

⚠ **UNVERIFIED with a publicly-trusted certificate.** This run used a locally
issued stand-in for `itms.toeshee.com`, so the *wiring* is proven — Traefik
loads the two files from the `certs/` bind mount and serves them — but no
Let's Encrypt chain was exercised. `certs/` is gitignored (`.gitkeep` keeps the
directory), so a `git pull` never touches your key.

```sh
docker compose up -d
docker compose ps -a --format 'table {{.Service}}\t{{.Status}}'
```

```
SERVICE           STATUS
api               Up 15 seconds
gateway           Up 22 seconds
keycloak          Up 26 seconds
migrate           Exited (0) 23 seconds ago
minio             Up 26 seconds (healthy)
openbao           Up About a minute (healthy)
openbao-init      Exited (0) 25 seconds ago
postgres          Up 2 minutes (healthy)
postgres-grants   Exited (0) 23 seconds ago
redis             Up 26 seconds
traefik           Up 15 seconds
web               Up 26 seconds
worker            Up 15 seconds
```

The four `Exited (0)` services are the one-shot jobs; that is success.

**Verify Traefik actually picked up your certificate.** The fallback to a
self-signed certificate is silent:

```sh
echo | openssl s_client -connect itms.toeshee.com:443 -servername itms.toeshee.com 2>/dev/null \
  | openssl x509 -noout -issuer -subject
```

```
issuer=CN = sandbox test CA
subject=CN = itms.toeshee.com
```

Your issuer will be your CA. **`issuer=CN = TRAEFIK DEFAULT CERT` means it fell
back** and your certificate is not in use — check the filenames and that the
key is readable by the container.

And the agent-facing side, which must be the *internal* CA:

```sh
echo | openssl s_client -connect itms.toeshee.com:8443 2>/dev/null \
  | openssl x509 -noout -issuer -subject
```

```
issuer=CN = it-suite internal CA
subject=CN = itms.toeshee.com
```

Two different issuers on the two ports is the correct picture. If `:8443` shows
your public CA, an agent will never connect.

### Step 8 — Claim the installation

**This replaces what used to be three steps: creating the first administrator by
piping Python into `docker compose exec`, inserting the organization row by SQL,
and granting the role by a second SQL statement.** All three now happen in one
transaction behind one screen, through the same service code the Users tab uses.

First confirm the realm imported and the init job finished:

```sh
docker compose logs keycloak | grep -i "imported"
docker compose ps -a keycloak-init      # Exited (0)
```

```
keycloak-1  | Realm 'it-suite' imported
```

**A non-zero `keycloak-init` means the setup screen will refuse**, because the
credential it creates accounts with does not exist yet. Re-run it — it is
idempotent — before going further:

```sh
docker compose up keycloak-init
```

**Configure email first if you can.** The screen below finishes by *emailing* the
administrator it creates, and an installation with no working SMTP finishes it
with a detour through the break-glass tunnel. [Keycloak
SMTP](#keycloak-smtp--required-before-user-management-works-at-all) below is ten
minutes and removes that entirely.

Then:

1. **Open `https://itms.toeshee.com/`.** On an installation with no users it
   shows the setup screen rather than redirecting to sign-in. That first request
   is also what **generates** the install code — nothing runs earlier, so do this
   before going to look for it.

2. **Read the code.** The api logs it exactly once, when it is created, and
   writes it to `/var/lib/its/secrets/setup-code` mode 600:

   ```sh
   docker compose logs api | grep -i "install code"
   ```

3. **Enter it**, with your name, email and organization name.

That creates the organization, the administrator account and its `org-admin`
grant **in one transaction** — either all three or none — and writes every part
of it to the hash-chained audit log.

**The organization's slug is `ITS_DEFAULT_ORG_SLUG` from `.env`** — `toeshee`
in the shipped example — not something you type, and not derived from the name
you enter unless that setting is blank. That is deliberate:
`app/core/security.py` resolves a token with no `org` claim to that slug, which
is every token on a single-organization installation, and an organization
created under any other slug is one its own administrator cannot sign in to.

**The install code is the only thing between a stranger and this box.** An
appliance answers on the network long before its owner sits down at it, and this
screen must answer before any user exists, so it cannot be gated by a permission.
Zero users alone is not a guard. Claim it promptly, from a browser you trust, and
treat the code as a password until you have.

### Step 9 — Finish enrolment and sign in

**Setup does not sign you in, and that is deliberate rather than unfinished.**
ITMS issues no session and holds no password; the account step 8 created has no
credential at all yet. The identity provider mails an **enrolment link** —
choose a password, enrol a passkey, then sign in normally.

So the last screen of setup says one of two things:

| It says | What it means |
|---|---|
| "Check that inbox for a link to choose a password, then sign in." | the mail was **accepted for delivery**. Not "arrived" — a relay can accept and still drop it silently |
| "The invitation email could not be sent…" | nothing is configured to send mail. The organization, the account and the grant all exist and are fine; there is simply no way to reach you |

**If the invitation could not be sent** — the expected outcome if you skipped the
SMTP step above — **re-running setup will not help.** The installation is
claimed, the screen is gone, and the claim route now answers "this installation
has already been set up". Recovery is vendor-side, through the tunnel in
[Break-glass: the identity admin
console](#break-glass-the-identity-admin-console-vendor-only):

- **Fix the cause, then re-send** (prefer this). Configure [Keycloak
  SMTP](#keycloak-smtp--required-before-user-management-works-at-all), then in
  the same console: **Users →** your account **→ Credentials → Credential
  Reset**, actions `Update Password` and `webauthn-register-passwordless`,
  **Send email**. That is the identical mail setup tried to send.
- **Get in first, fix it from inside**, if mail will take a while. Same console:
  **Users →** your account **→ Credentials → Set password**, **Temporary: On**.
  Sign in with it, then configure delivery in ITMS itself at **Settings →
  Email**, so no later account ever needs this tunnel.

Two things about **federated** accounts, which cost an afternoon each if you meet
them later rather than here:

- **Any attribute not declared in the realm's user profile is silently
  discarded** by Keycloak 26 — 201 Created, attribute gone. `org` is declared in
  `keycloak/realm-it-suite.json` under `components` →
  `org.keycloak.userprofile.UserProfileProvider`. Do not remove it.
- **`kcadm.sh ... -s 'attributes.org=toeshee'` silently does nothing.** It
  returns success and stores no attribute. Use the Admin REST API instead.

**`https://itms.toeshee.com/admin/` no longer reaches anything, and that is
the fix rather than the fault.** The identity provider's admin console can
create any user and reset any password in the fleet, so it is not routed on the
product's public hostname; Traefik forwards only what sign-in needs. Federating
AD/Entra and every other realm-level task is **VENDOR-ONLY** and goes through
the tunnel in [Break-glass: the identity admin
console](#break-glass-the-identity-admin-console-vendor-only) below.

⚠ **UNVERIFIED**: no browser session, no login and no AD/Entra federation has
been exercised against that console — through the tunnel or before it.

### Step 10 — Confirm you really are an administrator

**Do not skip this.** Under the old runbook the role grant was a separate SQL
statement and the classic failure was an account that authenticated and could do
nothing. The claim now grants `org-admin` in the same transaction, so the check
is quick — but "the code grants it" and "this deployment granted it" are
different claims, and only one of them is about your box.

In the console: **Settings → Users** lists your account with the **org-admin**
role. Or against the API, with a token from your signed-in session:

```sh
curl -H "Authorization: Bearer $TOKEN" https://itms.toeshee.com/api/v1/devices
```

```
GET /api/v1/devices -> 200
```

A `403 {"detail":"requires devices.read"}` means the role is not there. Seeded
roles are `super-admin`, `org-admin`, `technician`, `auditor`, `read-only` and
`requester`; a missing grant means the claim did not complete, and the fix is to
look at the api log for that request rather than to insert a row by hand.

### Step 11 — Prove the platform actually works

Everything so far only proves containers are running. This is the acceptance
test.

#### 11a — Authorization is enforced server-side

Unauthenticated, through the real front door:

```sh
for p in /api/v1/devices /api/v1/jobs /api/v1/audit/verify /api/v1/enrollment-tokens; do
  printf "  %-28s " "$p"; curl -s -o /dev/null -w "%{http_code}\n" https://itms.toeshee.com$p
done
```

```
  /api/v1/devices              401
  /api/v1/jobs                 401
  /api/v1/audit/verify         401
  /api/v1/enrollment-tokens    401
```

All four must be `401`. A `404` on all of them means **Traefik is not routing**
— see defect 7.

#### 11b — Create an enrollment token

```sh
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"first endpoint","expires_in_hours":24,"max_uses":1}' \
  https://itms.toeshee.com/api/v1/enrollment-tokens
```

```
  id 06a73b24-6786-7796-8000-5ef58999de3c name first endpoint max_uses 1
```

#### 11c — Install and start the agent on an endpoint

```sh
cd /opt/it-suite/agent && go build -o /usr/local/bin/its-agent ./cmd/agent

ITS_AGENT_ENROLL_URL=https://itms.toeshee.com \
ITS_AGENT_ENROLL_TOKEN=<the token value> \
ITS_AGENT_DATA_DIR=/var/lib/its-agent \
  /usr/local/bin/its-agent
```

```
level=INFO msg="its-agent starting" version=dev data_dir=/var/lib/its-agent enrolled=false
level=INFO msg=enrolling url=https://itms.toeshee.com/enroll/v1 hostname=vm os_family=linux
level=INFO msg=enrolled device_id=06a73b24-69ff-7320-8000-988575c84d1b gateway_url=wss://itms.toeshee.com:8443/agent cert_not_after=2027-08-05T21:59:34Z
level=INFO msg="gateway connected" url=wss://itms.toeshee.com:8443/agent
```

`enrolled` then `gateway connected` is the whole handshake: CSR signed by the
internal CA, certificate and CA pinned to `ITS_AGENT_DATA_DIR`, outbound mTLS
WebSocket established. The device appears immediately:

```sh
curl -H "Authorization: Bearer $TOKEN" https://itms.toeshee.com/api/v1/devices
```

```
   vm 06a73b24-69ff-7320-8000-988575c84d1b agent_status= online cert_state= ok
```

⚠ **UNVERIFIED**: only Linux. The agent cross-compiles to Windows and macOS and
`agent/packaging/` carries service installers, but no Windows or macOS endpoint
was enrolled in this run — and none has been since, by anybody on this project.
[`docs/modules/agent-distribution/first-run-verification.md`](../docs/modules/agent-distribution/first-run-verification.md)
is the procedure for changing that: what to run on one real machine of each
kind, what to observe at each step, and what each observation would falsify.

**If enrollment fails with `certificate and key do not form a pair: tls:
failed to find any PEM data in certificate input`,** you are running a build
from before the fix described in defect 11.

#### 11d — Round-trip a signed job

```sh
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"device_id":"<device id>","type":"hello","params":{}}' \
  https://itms.toeshee.com/api/v1/jobs
# ... then poll it
curl -H "Authorization: Bearer $TOKEN" https://itms.toeshee.com/api/v1/jobs/<job id>
```

```json
{
    "id": "06a73b25-dd24-7c95-8000-84ffd7bfca81",
    "type": "hello",
    "device_id": "06a73b24-69ff-7320-8000-988575c84d1b",
    "status": "succeeded",
    "attempts": 1,
    "params": { "message": "ping" },
    "result": { "greeting": "ping" },
    "issued_at": "2026-08-05T21:59:57Z",
    "started_at": "2026-08-05T21:59:57.920432Z",
    "finished_at": "2026-08-05T21:59:57.931670Z"
}
```

`"status": "succeeded"` with a `result` is the platform working: the server
signed the job envelope with the Ed25519 key that never leaves OpenBao, the
agent verified that signature before executing anything (there is no unsigned
command path), and the result came back up the same mTLS connection.

#### 11e — The audit chain

```sh
curl -H "Authorization: Bearer $TOKEN" https://itms.toeshee.com/api/v1/audit/verify
curl -H "Authorization: Bearer $TOKEN" "https://itms.toeshee.com/api/v1/audit?limit=20"
```

```
{"ok":true,"count":6,"broken_at":null}

  2026-08-05T21:59:26  user   auth.login                   user
  2026-08-05T21:59:34  user   enrollment.token_created     enrollment_token
  2026-08-05T21:59:34  agent  enrollment.device_enrolled   device
  2026-08-05T21:59:35  agent  gateway.connected            device
  2026-08-05T21:59:57  system jobs.dispatched              job
  2026-08-05T21:59:57  agent  jobs.succeeded               job
```

`ok: true` over a chain that includes the dispatch and the result is the
end-to-end proof. **First boot is complete.**

The MinIO bucket (`ITS_S3_BUCKET`) needs no provisioning step — `ObjectStore`
creates it on first use.

---

## Break-glass: the identity admin console (VENDOR-ONLY)

**This is not an operator task and it is not part of normal operation.**
Everything a customer does day to day — adding a person, resetting a password,
configuring email, branding sign-in — belongs in ITMS itself. Reaching for this
section to do routine work is a product gap worth filing, not a workaround
worth repeating.

**The identity admin console is no longer routed on `:443`, deliberately.**
`deploy/docker-compose.yml` routes only `/realms`, `/resources` and `/js` to
the identity container: what sign-in needs, and nothing else. `/admin` used to
be in that list and was removed, because that console can create any user and
reset any password in the fleet — the whole estate's authority in one page,
behind one password, on the same hostname the product answers on.
`backend/tests/test_public_surface.py` pins both halves of the decision, so
re-adding the prefix fails the suite instead of shipping quietly, and removing
one of the other three fails it too.

This supersedes GO-LIVE's "restrict `/admin` to an admin network or VPN before
go-live". There is nothing left on `:443` to restrict.

### Realm drift: what reaches an existing host, and what never does

Keycloak starts with `--import-realm`, which **skips a realm that already
exists**, so `deploy/keycloak/realm-it-suite.json` is imported once per host.
Everything after that reaches a running realm only through `keycloak-init`,
which `./scripts/deploy.sh` runs on **every** deploy. It converges an explicit
allowlist, writing only what differs, and nothing else
(`deploy/keycloak/init-clients.py`):

| Owned by the repo (converged every deploy) | Where it is listed |
|---|---|
| `loginTheme`, `internationalizationEnabled`, `supportedLocales`, `defaultLocale`, `displayName`, `displayNameHtml`, `editUsername`, `registrationAllowed`, `bruteForceProtected`, every `webAuthnPolicyPasswordless*` key the file sets | `CONVERGED_REALM_KEYS` |
| `enabled` and `defaultAction` of every required action the file lists, in both directions | `required_actions_to_update` |
| `account-console.enabled` (off), `account.enabled` (on), `it-suite-web` redirect URIs, `rootUrl`, `directAccessGrantsEnabled`, PKCE and post-logout attributes | `CONVERGED_CLIENTS` |
| the `browser-passkey` flow, bound only if a password path survives | `converge_passkey_flow` |

**Never written by the job**, whatever the file says: `smtpServer` (Settings →
Email owns it), the event-log settings, users, identity providers and any key
not in the table. `RUNTIME_OWNED_REALM_KEYS` names the ones a test keeps off
the allowlist. To make the repo own a new setting, add it to the file **and**
to the allowlist; a key in the file alone reaches new installs only.

**Check what a deploy changed:**

```sh
cd /opt/it-suite/deploy
docker compose logs keycloak-init | grep 'identity init'
```

`realm keys converged=nothing`, `updated=nothing` and `clients
corrected=nothing` mean the host already matched. The first deploy with this
job closes an account console that is still open and prints
`clients corrected=['account-console']`.

### The account console is closed at the realm, not at the router

`/admin` could be dropped from the routing rule. The **account console** could
not: it lives at `/realms/it-suite/account`, under the one prefix sign-in cannot
do without, so removing it by path would take the login pages and the OIDC
endpoints with it.

It is therefore closed one level down — `deploy/keycloak/realm-it-suite.json`
ships the built-in `account-console` client **disabled**. Nothing can enter that
portal, because the browser's authorization request names a client the realm
refuses. Everything it used to be needed for — set a password, add or replace an
MFA device, register a passkey — is a button on the ITMS profile page, which runs
exactly that one step on the same sign-in pages and returns the user to the page
they left.

Two consequences worth knowing before you go looking for them:

- The realm also declares the `account` client, deliberately, and leaves it
  **enabled**. That is not an oversight: the admin API resolves it as the system
  client for the action-token mail that enrols a user and resets a password. The
  pair is declared together because declaring only one of them makes the realm
  **unimportable** — Keycloak creates its own built-in pair when the file has no
  `account` client, and then collides with the file's `account-console` on a
  duplicate client id. `backend/tests/test_console_auth_realm.py` pins that,
  because the failure surfaces as a container that will not start, days later, on
  an unrelated `docker compose up -d`.
- End users no longer hold the `account` client's roles, so the account REST API
  answers them with a refusal as well. Nothing in ITMS calls it.

**Break-glass, if a vendor ever needs that portal:** inside the SSH tunnel,
Admin console → **Clients → `account-console` → Enabled → Save**. Turn it back
off afterwards; a customer who finds it has found a second product.

### Reaching it anyway

The identity container publishes **no host port**, so there is no
`localhost:8080` on the server to forward to. The tunnel forwards to the
container's own address on the compose network, which the SSH server resolves
for you.

```sh
# 1. On the server, in /opt/it-suite/deploy: the container's address on the
#    compose network.
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
  "$(docker compose ps -q keycloak)"
```

```
172.18.0.7
```

```sh
# 2. From your workstation: forward a local port to that address. Nothing is
#    published on the server and nothing is opened to the internet — the
#    forward lives and dies with this SSH session.
ssh -N -L 8080:172.18.0.7:8080 you@itms.toeshee.com
```

Then browse **`http://127.0.0.1:8080/admin/`** while that session stays open and
sign in with `KEYCLOAK_ADMIN_USER` / `KEYCLOAK_ADMIN_PASSWORD` from `.env`.
`--hostname-strict=false` in the container's command is why the console answers
on a hostname it was never configured with; without it the console would
redirect you to the public name and straight back out of the tunnel.

**Close the tunnel when you are finished, because the tunnel is the access
control** — the only one. That account has no second factor and there is no
network boundary behind it.

The container's address changes whenever the stack is recreated (any
`docker compose up -d` after an upgrade), so re-run step 1 each time. A stale
address presents as a tunnel that connects and then hangs, not as an error.

If you need to run a command rather than click one, skip the tunnel entirely —
this needs no forwarding at all:

```sh
docker compose exec keycloak /opt/keycloak/bin/kcadm.sh --help
```

---

## Branding and extending sign-in (VENDOR-ONLY)

> **Every "Admin console →" step in this section runs inside the SSH tunnel
> above, not on the public hostname.** The console is not routed on `:443`;
> `https://itms.toeshee.com/admin/` answers with the ITMS console's own
> catch-all, not with an identity provider. Open the tunnel first, then read
> "Admin console →" as `http://127.0.0.1:8080/admin/`.

**Realm import runs only against an empty database,** and that fact used to
own this section: everything in `deploy/keycloak/realm-it-suite.json` applied
at first boot, editing it on a running install changed nothing, and what
followed here was a list of the same changes re-entered by hand in a second
admin console.

**Most of that is now the `keycloak-init` job's work** (2026-08-20). It reads
the same realm file Keycloak imports — bind-mounted read-only — and converges
an **allowlist** of realm state onto a realm the import will never touch
again, on every `docker compose up`:

| Converged automatically | Where it comes from |
|---|---|
| Registered + enabled required actions | the file's `requiredActions`, honouring each entry's own `enabled` |
| Login theme | `loginTheme` |
| WebAuthn passwordless policy | every `webAuthnPolicyPasswordless*` key |
| Whether a user may change their own username | `editUsername` |
| `it-suite-web`'s `rootUrl`, `redirectUris`, post-logout URIs | that client's entry |

**An allowlist, not the whole representation, and the reason is worth
knowing.** `Settings → Email` writes the realm's `smtpServer` through the same
`manage-realm` grant this job holds. A convergence that carried the file's
whole realm would revert your mail configuration on every deploy, and the
symptom — "email stopped working again" — names nothing that leads back to the
upgrade. `backend/tests/test_keycloak_init_job.py::test_the_smtp_block_is_never_converged`
is the guard on that list.

**What this fixed, concretely.** On the first live deployment, *Change
password* and *Add a passkey* in the console's profile page did nothing: the
page reloaded and returned. The realm file that built that install listed one
required action, and Keycloak's import does not merge a supplied
`requiredActions` array with the defaults — if it is present, it *is* the set.
`kc_action` naming an action the realm has not registered is ignored. Sign-out
dead-ending on "Invalid redirect uri" was the same fact: the live client still
carried `redirectUris: ["https://*/auth/callback"]`, and a wildcard is honoured
only as a trailing `*`.

Two things below are still **by hand**, and neither is an oversight — see each.

### 0. Check the flow after any theme change — and how to get back

**A FreeMarker error in a login template answers 500, and nobody can sign in
— including you.** On a deployment with one administrator that is a lockout,
and it is reached by a typo. The templates are written without a Keycloak to
render them against (`backend/tests/test_console_auth_realm.py` checks that
every block directive closes and that every `itms-` class has a rule, which
catches the gross cases and proves nothing about whether a page renders).

So after any deploy that touches `deploy/keycloak/themes/`:

1. **Keep your current session open.** Do the check in a private window, so a
   broken page cannot take your only authenticated tab with it.
2. Walk the flow: sign in (password → authenticator code), then from the
   console's profile page run **Change password** and **Manage MFA device**.
3. If a page 500s or renders wrong, the theme is a bind-mount and the rollback
   is two commands:

```sh
cd /opt/it-suite && git checkout HEAD~1 -- deploy/keycloak/themes/
cd deploy && docker compose restart keycloak
```

**`cd deploy` is not optional and its absence broke this rollback in anger.**
`docker-compose.yml` lives in `deploy/`, so running `docker compose` from the
repository root answers `no configuration file provided: not found` — which,
read at speed with a broken login page in the other window, looks like the
rollback itself failing rather than the shell being in the wrong directory.

That reverts the templates and nothing else — no image rebuild, no migration,
no restart of the application. `git checkout <last-good-sha> -- deploy/keycloak/themes/`
if you need to go further back than one commit.

### 1. The ITMS login theme — automatic

Converged from the realm file's `loginTheme`; the theme itself is mounted at
`./keycloak/themes:/opt/keycloak/themes:ro`. Nothing to do but deploy.

**It styles the whole flow as of 2026-08-20, not just the first page.**
`parent=base` gives the theme templates and no styling at all, so until that
date exactly one page was branded — `login.ftl` — and every other page a user
reaches rendered as bare HTML on a grey background: the authenticator code on
every sign-in, enrolling a device, changing a password, and the three pages you
land on when something goes wrong. The operator photographed the code step one
day after go-live. Seven templates are themed now; the two WebAuthn pages are
deliberately left on base (they carry live JavaScript that must not be copied
without a real Keycloak to test against) and a test holds that decision in
place.

To apply it to a realm that predates the convergence job, run the job:

```sh
docker compose up keycloak-init
docker compose restart keycloak
```

Check it in a private window at `https://itms.toeshee.com/` — the console has
no launcher of its own, so an unauthenticated visit redirects straight to the
sign-in page, which is what the login theme actually styles.
**An unbranded page means the mount is missing** — Keycloak falls back to its
own theme silently and logs nothing about it.

> This check used to say `/realms/it-suite/account`, the account console. That
> URL no longer renders a usable page: the `account-console` client is shipped
> **disabled** in the realm (see the break-glass section above), so the browser's
> authorization request for it is refused. Checking branding there would now
> tell you nothing about the theme, and the sign-in page is the honest test
> anyway — it is the page every user sees.

### 2. Passkeys — policy automatic, sign-in flow by hand

The **policy** and the **enrolment required action** are converged from the
realm file, so *Add a passkey* on the profile page works after a deploy:

| Field | Value | Converged? |
|---|---|---|
| Relying Party Entity Name | `ITMS` | yes |
| Signature Algorithms | `ES256`, `RS256` | yes |
| Attestation Conveyance Preference | `none` | yes |
| Require Discoverable Credential | `Yes` | yes |
| User Verification Requirement | `required` | yes |
| `webauthn-register-passwordless` registered and enabled | — | yes |

**Both halves are automatic as of 2026-08-20**, and this section's warning has
changed rather than gone away. `keycloak-init` copies the stock `browser` flow
to `browser-passkey`, adds the WebAuthn passwordless authenticator as an
**ALTERNATIVE** beside the existing legs, and binds it.

**Why an operator should still read this.** Binding a browser flow is the most
dangerous write this platform makes: a flow that refuses everybody refuses the
account that would repair it, and the way back is `kcadm.sh` inside the
container. Four things contain that, and the third is the one that matters:

1. the flow is a **copy** of Keycloak's own, so the password leg is what the
   vendor shipped rather than a graph reconstructed by us;
2. passkey is an **alternative beside** the password form, never in front of
   it, so a WebAuthn execution that cannot run falls through;
3. the job **refuses to bind** unless it can see a reachable password
   alternative in the flow it just built — a measurement of the result, not a
   precaution taken while writing (`password_alternative_survives`, and note
   that a password form nested inside a DISABLED subflow does not count);
4. a realm already bound to this flow is left alone, so the daily deploy is not
   rewriting the mechanism every account depends on.

**Check it in a private window with your session still open**, as with any
theme change. If sign-in misbehaves, unbind without a shell in the container:

```sh
docker compose exec -e PGPASSWORD="$(grep '^POSTGRES_ADMIN_PASSWORD=' .env | cut -d= -f2-)" \
  postgres psql -U its_admin -d keycloak \
  -c "UPDATE realm SET browser_flow = (SELECT id FROM authentication_flow
       WHERE alias = 'browser' AND realm_id = (SELECT id FROM realm WHERE name = 'it-suite'))
      WHERE name = 'it-suite';"
docker compose restart keycloak
```

That puts the realm back on the stock browser flow — password and OTP, no
passkey — which is where it was before this feature existed.

---

**The older manual route, kept because it is still how you would inspect what
the job built:** **Authentication → Flows → browser → Duplicate**
(call it `browser-passkey`), add a **WebAuthn Passwordless Authenticator**
execution as an **Alternative** beside the Username/Password form, and only then
bind it as the realm's Browser flow.

> **Test it with a real authenticator before you bind it.** Binding a broken
> browser flow locks every account out of the console, including yours, and the
> only recovery is `kcadm.sh` inside the Keycloak container. This is why the
> flow is neither in the committed realm JSON nor in the convergence
> allowlist — a wrong `authenticationFlows` graph does not fail loudly, it
> fails by locking you out, and a job that re-applied it on every deploy would
> do so unattended at 3am.

`User verification: required` is deliberate. It makes the passkey two factors on
its own — the authenticator, plus the PIN or biometric that unlocks it — which
is why a passkey sign-in does **not** additionally prompt for TOTP. Password
sign-in still does.

The consequence worth stating: **an account with a passkey and no TOTP is
protected only as well as that one authenticator.** Lose the phone, lose the
account, and recovery is an admin resetting credentials. Keep TOTP enrolled as
well on anything holding `org-admin` or `super-admin`.

### 3. Sign in with Microsoft — by hand, for now

Not converged, because the client secret is the obstacle: it cannot live in
the realm file (it would be a credential in git), so the identity provider
cannot be expressed there in full. Converging it means teaching the job to
read `ENTRA_CLIENT_SECRET` and create the provider and its `org` mapper — a
cycle of its own, tracked in the UX remediation programme's backlog.

In Entra: register an application, add the redirect URI

```
https://itms.toeshee.com/realms/it-suite/broker/microsoft/endpoint
```

and mint a client secret.

Admin console → **Identity providers → OpenID Connect v1.0**:

| Field | Value |
|---|---|
| Alias | `microsoft` — **must match**, the redirect URI above contains it |
| Discovery endpoint | `https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration` |
| Client ID | the Entra application (client) id |
| Client authentication | `Client secret sent as post` |
| Client Secret | yours, from `.env` (`ENTRA_CLIENT_SECRET`) — **type it here, never into the realm JSON** |
| Trust Email | **Off** |
| First login flow | `first broker login` |

Then **Mappers → Add mapper → Hardcoded attribute**: attribute `org`, value your
org slug (the same one as `ITS_DEFAULT_ORG_SLUG` in `.env`).

**The mapper is not optional, and its absence is masked.** With
`ITS_MULTI_TENANT=false` a federated user carrying no `org` attribute falls back
to the default org — so sign-in *works*, and every Microsoft account silently
lands in one tenant. Verify the **claim**, not the login: sign in through
Microsoft once, then in the browser's devtools console on the console page:

```js
const k = Object.keys(sessionStorage).find(k => k.startsWith("oidc.user"));
JSON.parse(atob(JSON.parse(sessionStorage[k]).access_token.split(".")[1])).org
```

That must print your org slug. `undefined` means the mapper is missing and the
fallback is carrying the login.

**Trust Email off is deliberate.** With it on, a Microsoft identity that merely
claims an existing user's email address is linked to that account
automatically — whoever controls that name at the IdP inherits the local
account's roles.

---

## Language: English and Spanish

The console speaks English and Spanish. Each person picks their own language
from the account chip (top right): **Language / Idioma**, between **Settings**
and **Sign out**. **Your profile** has the same choice. The choice is saved on
the person's account, not the browser, so it follows them to every browser and
to the self-service portal. Nothing is set per organization.

What follows the choice:

- every screen of the console and the portal;
- the messages the server writes: errors, refusals and confirmations;
- notification emails, which go out in the **recipient's** own language. An
  address that is not a user of that organization gets English;
- the Keycloak sign-in pages and Keycloak's own emails (invitations, password
  resets). For these, the realm must have languages turned on. `keycloak-init`
  does this on every deploy (`internationalizationEnabled`, `supportedLocales`,
  `defaultLocale`; see the table above). Changing language in the console also
  writes the person's Keycloak `locale` attribute. If that write fails, the
  console setting is still saved, and the person sees a note saying the sign-in
  pages were not updated.

What stays English, on purpose:

- **Audit log entries as recorded.** The log is hash-chained, so a stored entry
  is never rewritten. The audit screens translate their own labels and the
  action names, not the text inside an entry.
- **Text stored by background work**, such as a job's failure reason, a
  restart's reason or a sweep's error. It is stored in English. Where the
  console maps a stored code to words, those words are translated.
- **Server logs**, the agent, and its logs (`msi.log`, service messages).
- **The API's machine-readable description** (OpenAPI), wire values, and CSV
  column headers.

The Spanish was written by Claude and has not yet been reviewed by a native
speaker. Every place a term was chosen between alternatives is in the glossary
at the top of `frontend/src/i18n/es/index.ts`, so one change there corrects it
everywhere.

---

## Forwarding Keycloak's credential events

**Optional, and the deployment tells you which way you chose.** A passkey
registered, a passkey removed, a Microsoft identity linked to an account — each
is a new way into a privileged account, and each is recorded by Keycloak in a
log that is not tamper-evident and not the one an auditor reads. Turning this
on copies those four events into ITMS's hash-chained audit log, hourly.

> **VENDOR-ONLY.** Every "Admin console →" step below runs inside the SSH
> tunnel in [Break-glass: the identity admin
> console](#break-glass-the-identity-admin-console-vendor-only). That console is
> not routed on `:443`.

Leave it off and nothing breaks: the sweep is a no-op and **Platform → Status**
reports `keycloak_events` as `ok` with `configured: false`. That wording is
deliberate — you have not broken anything, but the gap is on the page rather
than nowhere.

**What this does not make true.** Keycloak's own log remains the source. A
Keycloak compromised *before* a sweep runs can drop an event this will never
see, and nothing here makes that absence detectable. Forwarding puts the events
that are emitted into a tamper-evident chain; it does not make the emitter
trustworthy.

### 1. Turn the event log on — it is OFF by default

Admin console → **Realm settings → Events**.

| Tab | Setting | Value |
|---|---|---|
| User events config | Save events | **On** |
| User events config | Expiration | at least `2 days` (the sweep's own window is 30 days; this is Keycloak's, and it must outlive an outage) |
| Admin events config | Save events | **On** |

**Without this the feature has nothing to read and sits at "configured, zero
events" for ever** — a status page that looks healthy and a chain that stays
empty. Check the **User events** tab shows rows before going further.

### 2. The service-account client — already created for you

`deploy/keycloak/init-clients.py` runs as the `keycloak-init` job on every
`docker compose up` and creates this client, so there is nothing to click:

| Field | Value |
|---|---|
| Client ID | `it-suite-events` — matches `ITS_KEYCLOAK_EVENTS_CLIENT_ID` |
| Client authentication | **On** (confidential) |
| Authentication flow | **Service accounts roles** only — standard flow and direct access grants both off |
| Role | from `realm-management`: **`view-events`**. Nothing else |
| Secret | `/var/lib/its/secrets/keycloak-events.secret`, mode 600, written by the same job |

**Only `view-events`, and this is the part that matters.** Not `realm-admin`,
and not the bootstrap `admin` account from `.env` (`KEYCLOAK_ADMIN_USER` /
`KEYCLOAK_ADMIN_PASSWORD`): that account can reset any password and create any
user, which is why the init job **rotates its password away** to a value only
the secrets volume holds ([`GO-LIVE.md`](GO-LIVE.md) §2). A credential that can
only *read* an event log has a worst case of an information leak; one that can
administer the realm has a worst case of every account in it. If a future
version of this feature needs a second role, that is a change to what this
credential can do and it is a reviewed decision, not a silent upgrade —
`backend/tests/test_keycloak_init_job.py` fails if the widening that gave
`it-suite-provisioning` its second role ever reaches this one.

### 3. The secret, and where it lives

The job writes it to `/var/lib/its/secrets/keycloak-events.secret`, mode 600, on
the same `its-secrets` volume as the audit HMAC key and for the same reason: the
worker reads it off disk, and an env var holding a credential is readable by
`docker inspect` and lands in shell history.

It is re-read and re-written on **every** run, not only on creation, so a
restore that brought back the database but not the secrets volume is repaired by
`docker compose up keycloak-init` rather than by a trip to the admin console.

### 4. The settings, which ship correct

```
ITS_KEYCLOAK_ADMIN_URL=http://keycloak:8080
ITS_KEYCLOAK_REALM=it-suite
ITS_KEYCLOAK_EVENTS_CLIENT_ID=it-suite-events
ITS_KEYCLOAK_EVENTS_SECRET_PATH=/var/lib/its/secrets/keycloak-events.secret
```

All four are the shipped values in `deploy/.env.example`. `http://keycloak:8080`
is the in-network address — the sweep runs in the worker container and does not
need to go out through Traefik — and it is **shared with the provisioning
client**, so blanking it to turn forwarding off also turns user provisioning off
and breaks the first-boot setup screen. Turn forwarding off at step 1 instead,
by leaving the realm's event log off.

```sh
docker compose up -d worker api
```

### 5. Check it actually ran

**Platform → Status** should show `keycloak_events` as `ok` with
`configured: true` within an hour. Until the first sweep completes it reads
`degraded / stale` with `ever_run: false`, which is the honest answer.

Then prove the whole path rather than the configuration: register a passkey on
a test account (**Branding and extending sign-in → Passkeys** above), wait for
the next sweep, and look for `auth.passkey_registered` in the audit log.

```sh
curl -sS -H "Authorization: Bearer $TOKEN" \
  "https://itms.toeshee.com/api/v1/audit?action=auth.passkey_registered" | jq
```

**A configured deployment whose secret file is missing reports `down` /
`misconfigured`, not `configured: false`.** Those are different failures: the
second is a choice, the first is a deployment whose operator believes
forwarding is running.

### What is not covered

* **Only four event types are forwarded**: passkey registered, passkey removed,
  identity linked, identity unlinked. Not logins — `auth.login` is already
  written by the API when a token is presented, and forwarding every login in
  the realm would bury the four that matter.
* **An event whose Keycloak user does not resolve to an ITMS user is counted,
  not filed.** The realm is shared by every tenant, and filing one tenant's
  credential change into another's evidence is worse than not forwarding it.
  These show in the task's `unresolved` count.
* **Nothing here has been run against a live Keycloak.** The sweep, the
  mapping and the dedupe are covered by tests; the HTTP conversation with
  Keycloak's admin API is not.

---

## Keycloak SMTP — required before user management works at all

> **SUPERSEDED BY THE PRODUCT FOR EVERY DAY BUT THE FIRST.** Email is now
> **Settings → Email** inside ITMS — host, port, TLS mode, from address,
> credentials and a test send, with the password written straight through to the
> realm and never stored or echoed back by ITMS. That is where an operator
> configures delivery, and this section no longer describes their work.
>
> **It still describes yours, in exactly one situation: first boot.** Settings →
> Email is behind a sign-in, and on a brand-new installation nobody can sign in
> until the enrolment mail arrives — which needs SMTP. Configuring it here, once,
> before [Step 8 — Claim the installation](#step-8--claim-the-installation),
> breaks that circle. Doing it afterwards is the recovery in
> [Step 9](#step-9--finish-enrolment-and-sign-in).
>
> **VENDOR-ONLY either way.** The "Admin console →" step below runs inside the
> SSH tunnel in [Break-glass: the identity admin
> console](#break-glass-the-identity-admin-console-vendor-only); that console
> is not routed on `:443`.

**Not optional, and the failure it prevents is unrecoverable from the
console.** ITMS creates a user in Keycloak and then asks Keycloak to email
that person an action link so they can set a credential and enrol a passkey.
A created user has **no credential** until they follow that link. If Keycloak
cannot send mail:

- the account exists, is enabled, and **nobody can ever sign in to it**;
- there is **no admin API in Keycloak 26 that returns the action link** for an
  operator to copy out and pass on by hand — the link is generated inside the
  send and is not exposed anywhere;
- the only remedy is to configure SMTP and use the console's **Resend
  invitation** action — or, vendor-side, to set a temporary password by hand in
  the identity admin console over the SSH tunnel.

**The first user this bites is the first administrator**, created by the setup
screen in step 8 before anyone can sign in to fix it. So configure this before
you claim the installation, not after you have created ten accounts nobody can
reach.

### 1. Fill in the Email tab

Admin console → **Realm settings → Email**.

| Field | Value | Why it matters |
|---|---|---|
| From | e.g. `itms@toeshee.com` | Keycloak refuses to send with this empty, and the error surfaces to the operator as a failed invitation, not as a config error |
| From display name | e.g. `ITMS` | what the recipient sees; an unrecognised sender is a link nobody clicks |
| Reply to | a monitored mailbox | the invitation is the one mail where "reply to this" is a real support path |
| Host / Port | your relay | `587` with StartTLS, or `465` with SSL |
| Enable StartTLS **or** Enable SSL | on | one of the two, not both |
| Authentication | on, with username and password | the password is typed **here**, in the running realm — see below |

Then use **Test connection** on that same tab, with your own address, and
**wait for the mail to arrive**. A green "Success" means Keycloak reached the
relay; it does not mean the relay delivered anything, and a relay that accepts
and then silently drops mail from an unknown sender is the common case.

### 2. The password does not go in the realm JSON

`deploy/keycloak/realm-it-suite.json` **cannot carry the SMTP password**, and
must not be edited to. That file is in **git**: a secret committed there is a
credential in every clone, every fork and every CI cache, rotatable only by
rewriting history. It is the same argument the "Sign in with Microsoft" section
above makes about `ENTRA_CLIENT_SECRET`, at the second field somebody reaches
for.

There is a second reason the file is the wrong place regardless of secrets:
**realm import runs only against an empty database**, so editing the JSON on a
running install changes nothing at all. The Email tab is the only thing that
affects a live realm.

### 3. What this does NOT achieve

- **It does not make invitations reliable.** ITMS reports a send failure in the
  create-user response rather than swallowing it, and offers a resend — because
  one transient mail failure would otherwise leave an account nobody can ever
  sign into. Watch for that in the response; it is not a background job you can
  ignore.
- **It does not verify the address.** Keycloak sends to whatever address the
  operator typed. A typo produces a successful send and an account that stays
  uncredentialed for ever.
- **Nothing here has been run against a live Keycloak.** The invitation path is
  covered by tests against a mock transport, exactly as `keycloak_events` is.

### 4. Signing in to Microsoft 365 instead of with a password

Microsoft is retiring password sign-in (basic auth) for SMTP. **Settings →
Email → Sign-in: Microsoft 365** makes the realm sign in to
`smtp.office365.com` with an Azure application instead. The realm fetches the
token itself (Keycloak 26.2 XOAUTH2, client-credentials grant); ITMS never
holds it. The client secret is written straight through to the realm like the
password, and is never stored or shown by ITMS.

> ⚠ **UNVERIFIED against a live Microsoft tenant.** The permission, the scope
> and the Exchange commands below follow Microsoft's documentation
> (*Authenticate an IMAP, POP or SMTP connection using OAuth*, revision of
> 2025-10-17). Everything ITMS sends is tested against a fake. Until steps 9
> and 10 have been run, this is "shipped, not verified".

**Use a separate Azure application for this**, not the one Email Intake uses:
this secret lives in the realm, and it should not also unlock reading mail.

Azure portal (Microsoft Entra admin center):

1. **App registrations → New registration.** Name it `ITMS outgoing mail`,
   choose *Accounts in this organizational directory only*, leave *Redirect
   URI* empty, and press **Register**.
2. On its **Overview** page, note the **Application (client) ID** and the
   **Directory (tenant) ID**.
3. **API permissions → Add a permission → APIs my organization uses**, search
   for `Office 365 Exchange Online`, choose **Application permissions**, tick
   **SMTP.SendAsApp**, and press **Add permissions**.
4. Still on **API permissions**, press **Grant admin consent for** your
   organisation and confirm. The status column must read *Granted*.
5. **Certificates & secrets → New client secret.** Pick an expiry, press
   **Add**, and copy the **Value** column now (it is shown once). Put a
   reminder in your calendar a week before it expires: when it expires,
   invitations and password resets stop sending.
6. **Enterprise applications** (not App registrations) → open `ITMS outgoing
   mail` → note its **Object ID**. It is a different value from the Object ID
   on the App registrations page, and using the wrong one makes sign-in fail.

Exchange Online PowerShell, as an Exchange administrator. Each `Read-Host` line
asks you to paste a value; nothing needs editing before you paste the lines:

```powershell
Install-Module -Name ExchangeOnlineManagement -Scope CurrentUser
Import-Module ExchangeOnlineManagement
Connect-ExchangeOnline
$AppId    = Read-Host "Application (client) ID from step 2"
$ObjectId = Read-Host "Enterprise application Object ID from step 6"
$Mailbox  = Read-Host "The mailbox ITMS sends as (the From address)"
$Sp = New-ServicePrincipal -AppId $AppId -ObjectId $ObjectId -DisplayName "ITMS outgoing mail"
Add-MailboxPermission -Identity $Mailbox -User $Sp.Identity -AccessRights FullAccess
Add-RecipientPermission -Identity $Mailbox -Trustee $Sp.Identity -AccessRights SendAs -Confirm:$false
Get-CASMailbox -Identity $Mailbox | Format-List SmtpClientAuthenticationDisabled
```

The last line must print `SmtpClientAuthenticationDisabled : False` (SMTP AUTH
enabled for this mailbox). If it prints `True`, run
`Set-CASMailbox -Identity $Mailbox -SmtpClientAuthenticationDisabled $false`.

**Why there is no application access policy here.** An `SMTP.SendAsApp`
application reaches only the mailboxes granted to its Exchange service
principal with `Add-MailboxPermission` above: one mailbox, by construction.
`New-ApplicationAccessPolicy` scopes Microsoft Graph and EWS access and does
not govern SMTP or IMAP.

In ITMS:

7. **Settings → Email.** Mail server `smtp.office365.com`, port `587`,
   Encryption **StartTLS · port 587**, From address = the mailbox above.
8. **Sign-in: Microsoft 365.** Username = the same mailbox, then the
   Directory (tenant) ID, the Application (client) ID and the client secret.
   Leave *Advanced* alone. **Save.** If a password was saved before, the
   console tells you it is being replaced; it is not kept.
9. Press **Send a test message** and confirm it **arrives** in that mailbox.
   Accepted is not delivered: look in the inbox, not at the button.
10. Invite a test user and confirm the invitation arrives.

If step 9 fails with an identity-provider error, check in this order: admin
consent granted (step 4), the Enterprise application Object ID (step 6), SMTP
AUTH enabled for the mailbox, and the client secret not expired.

**Going back to a password:** choose **Sign-in: Password**, type the password,
Save. The Microsoft application details are removed from the realm in the
same save.

### 5. Notification email channels over Microsoft 365

Ticket and platform notifications are sent by ITMS itself, per organisation,
through **Notifications → Channels**. An email channel offers the same
**Sign-in: Microsoft 365**: the directory (tenant) ID, application (client) ID
and client secret go to ITMS's vault (never shown again), each send fetches a
token and signs in to `smtp.office365.com:587` with StartTLS, and every use is
in the audit log as `notification.credential_used`. Password sign-in stays for
any other mail server.

The Azure side is steps 1-6 above. The `ITMS outgoing mail` application can be
reused; to send from another mailbox, run the `Add-MailboxPermission` and
`Add-RecipientPermission` lines again with that mailbox. The channel's **From
address** is the mailbox it sends as. Press **Test** on the channel: the test
message goes to the From address itself. Unverified against a live tenant, as
above.

---

## Email intake from a Microsoft 365 mailbox

Exchange Online refuses IMAP with a password, so a Microsoft 365 support
mailbox signs in with **Connect with Microsoft**: a person signs in once as the
mailbox, and ITMS keeps the resulting refresh token in the vault and turns it
into a short-lived access token for each poll. Password IMAP keeps working
for every other mail server.

> ⚠ **UNVERIFIED against a live Microsoft tenant.** Everything ITMS sends is
> tested against a fake token endpoint and a scripted IMAP server. Until steps
> 12 to 15 below have been run, this is "shipped, not verified".

**Before anything else**, `ITS_PUBLIC_CONSOLE_URL` must be set in `.env` to
the https address people open the console at (for this deployment,
`https://itms.toeshee.com`). It is the address Microsoft returns the browser
to. Without it the console says Connect with Microsoft is switched off.

**Use a separate Azure application from the outgoing-mail one** in the
section above, so that the secret the identity provider holds cannot also read
mail.

Azure portal (Microsoft Entra admin center):

1. **App registrations → New registration.** Name it `ITMS email intake`,
   choose *Accounts in this organizational directory only*.
2. Under *Redirect URI* choose **Web** and enter the address the console shows
   under Email intake → **Microsoft 365 connection → Return address to
   register in Azure**. For this deployment that is
   `https://itms.toeshee.com/settings/email-intake/oauth/callback`. Press
   **Register**.
3. On its **Overview** page, note the **Application (client) ID** and the
   **Directory (tenant) ID**.
4. **API permissions → Add a permission → Microsoft Graph → Delegated
   permissions.** Tick **IMAP.AccessAsUser.All** and **offline_access**, and
   press **Add permissions**. Then press **Grant admin consent for** your
   organisation and confirm; both rows must read *Granted*.
5. **Certificates & secrets → New client secret.** Copy the **Value** now. Put
   a reminder in your calendar a week before it expires: when it expires, every
   Microsoft mailbox stops polling until a new one is saved in step 9.

These are **delegated** permissions: the app can only ever act as the account
that signed in during step 11, so there is no application access policy to
add. (`New-ApplicationAccessPolicy` scopes Graph and EWS app-only access; it
does not apply here.)

Exchange Online PowerShell, as an Exchange administrator. The `Read-Host` line
asks you to paste the mailbox address; nothing needs editing first:

```powershell
Connect-ExchangeOnline
$Mailbox = Read-Host "The support mailbox address"
Get-CASMailbox -Identity $Mailbox | Format-List ImapEnabled
```

6. It must print `ImapEnabled : True`. If it prints `False`, run
   `Set-CASMailbox -Identity $Mailbox -ImapEnabled $true`.

In the console:

7. Check that `docker compose exec api printenv ITS_PUBLIC_CONSOLE_URL` prints
   the address from step 2 without the `/settings/...` part.
8. **Email intake → Microsoft 365 connection** shows the same return address
   as step 2. If it says Connect with Microsoft is switched off, the console
   address is not set.
9. Enter the directory ID, the application ID and the client secret. **Save
   Microsoft 365 connection.**
10. **Add mailbox → Sign-in: Microsoft 365.** Address and username are the
    support mailbox; the IMAP server `outlook.office365.com`, port 993, is
    filled in. Choose the default category. **Save.** The mailbox shows **Not
    connected** and is switched off.
11. Press **Connect with Microsoft** on its row. Sign in **as the support
    mailbox's own account** (the account picker is shown on purpose) and
    accept. The console returns to a page saying **Connected**.
12. Edit the mailbox, switch it on (Active), save, and send it a test message
    from another account. Within one poll interval (two minutes) it must become
    a ticket.
13. Press **Test** on the row; it must say it connected and listed the folder.
14. **Revoke it on purpose:** sign in to https://myaccount.microsoft.com as the
    support mailbox, open **Apps**, and remove *ITMS email intake*. Within one
    poll interval the row must show **Microsoft sign-in expired** with a
    **Reconnect** button, and the logs must show no retry loop.
15. Press **Reconnect**, sign in again, and confirm polling resumes.

If step 11 ends at a Microsoft screen asking for administrator approval, step
4's admin consent was not granted. If it ends with `AADSTS50011`, the return
address in step 2 does not exactly match what the console shows in step 8.

**The one thing that stops it later:** the refresh token belongs to the person
account that signed in. If that account is disabled or deleted, an
administrator revokes its sessions, or (depending on tenant policy) its
password changes, the mailbox moves to **Microsoft sign-in expired** and needs
Reconnect. ITMS uses the token every poll, so it does not lapse from disuse
while the mailbox is switched on.

---

## The provisioning client

> **AUTOMATED — there is nothing to do here.** `deploy/keycloak/init-clients.py`
> runs as the `keycloak-init` job on every `docker compose up`: it creates this
> client, grants its roles, and writes its secret to
> `/var/lib/its/secrets/keycloak-admin.secret` mode 600. `deploy/.env.example`
> ships already pointing at that file. This section now documents **what the job
> did and what it costs**, which is the half that still needs a reader.

The credential ITMS uses to create, disable, delete and password-reset users in
Keycloak — and the one the first-boot setup screen uses to create the very first
administrator. **A different client from `it-suite-events`**, deliberately, and
this section spends all of its words on what it costs, because nobody has to
create it any more and the cost is permanent.

### 1. What the job created

| Field | Value |
|---|---|
| Client ID | `it-suite-provisioning` — matches `ITS_KEYCLOAK_ADMIN_CLIENT_ID` |
| Client authentication | **On** (confidential) |
| Authentication flow | **Service accounts roles** only — standard flow and direct access grants are both off, so a leaked secret is not a password grant |
| Roles | from `realm-management`: **`manage-users`** and **`manage-realm`**. Nothing else — not `realm-admin`, and never the bootstrap `admin` account from `.env` |
| Secret | `/var/lib/its/secrets/keycloak-admin.secret`, on the `its-secrets` volume, mode 600 |

`manage-realm` is the 2026-08-19 widening
(`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §4): it
is what lets **Settings → Email** inside ITMS write the realm's SMTP block, so
that configuring delivery is no longer a trip to somebody else's console. It
touches this client only — the event sweep stays `view-events`, and
`backend/tests/test_keycloak_init_job.py` asserts it.

**To check it, or to repair it:**

```sh
docker compose ps -a keycloak-init      # Exited (0)
docker compose up keycloak-init         # idempotent; re-running is its normal case
```

A secrets volume restored without the database, or the other way round, is
exactly the case the re-run repairs: the job re-reads and re-writes every client
secret on every run rather than only on creation.

### 2. What that grant actually is, stated plainly

**`manage-users` has no finer split in Keycloak.** There is no
"create-users-but-not-reset-passwords" role to assign instead. So:

> **This credential can create any user and reset any password in the realm.
> Whoever owns ITMS owns every identity in it.**

That is the price of the full user lifecycle in the console, and it is not
softened by anything below. The mitigations are real but they are mitigations,
not a narrower grant:

| Mitigation | What it actually buys |
|---|---|
| A separate client from `it-suite-events` | the hourly event sweep keeps its `view-events`-only worst case — an information leak — instead of inheriting this one |
| The code calls exactly six operations | create user, set enabled true/false, send `execute-actions-email`, delete user, set the caller's own `locale` attribute (`PATCH /me`, Spanish-language work), remove one AI app's consent for a user (revoking an app under Settings → AI connections; needs nothing beyond `manage-users`). **The credential stays broader than the code**; a bug or an injected call is not bounded by this row |
| Every route is RBAC-gated server-side | `users.manage` / `users.read`, checked in the API, never in the frontend |
| Every action is written to the hash-chained audit log | in the same transaction as the change, so an unlogged lifecycle action is a failed one |
| **Impersonation is never called** | impersonation is *also* inside `manage-users`. ITMS has no code path that calls it, and adding one would be a new decision, not a patch |

If a future version of this feature needs a second Keycloak role, that is a
change to what this credential can do, and it is your decision rather than a
silent upgrade.

### 3. The settings, which ship correct

```
ITS_KEYCLOAK_ADMIN_URL=http://keycloak:8080
ITS_KEYCLOAK_REALM=it-suite
ITS_KEYCLOAK_ADMIN_CLIENT_ID=it-suite-provisioning
ITS_KEYCLOAK_ADMIN_SECRET_PATH=/var/lib/its/secrets/keycloak-admin.secret
```

All four are the shipped values in `deploy/.env.example`; copy it and they are
already right. `ITS_KEYCLOAK_ADMIN_URL` is **shared with the event sweep** — the
realm's in-network address, not a second setting — and `http://keycloak:8080`
never goes out through Traefik, which is what keeps the admin API off the public
entrypoint.

**The path ships populated, and it used to ship commented out.** That reversal is
deliberate: the comment block it replaced told you to create the client by hand
and *then* uncomment, which described work the init job now does — while the
commented-out line quietly made a fresh installation refuse its own setup screen
with "identity provisioning is not configured", on the one occasion that screen
is ever shown.

**A short window on a cold host is expected.** Nothing gates the `api` service on
`keycloak-init` finishing (making it a dependency would turn an init failure into
a product that will not start at all), so for a few seconds the path is set and
the file is not there yet. In that window the setup screen says the installation
is still starting up and asks you to try again, and **Platform → Status** reports
`keycloak_events` as `down / misconfigured`. Both clear themselves. Neither is a
reason to edit the settings above — check `docker compose ps -a keycloak-init`.

### 5. What this does NOT achieve

- **It does not scope the credential to one tenant.** The Keycloak realm is
  shared by every ITMS organization. Tenant scoping of *who may press the
  button* is ITMS's RBAC; the credential itself sees the whole realm.
- **It does not let ITMS see or set a password.** Reset means asking Keycloak
  to email an `UPDATE_PASSWORD` action. No password is generated, stored,
  displayed or returned by ITMS — invariant 3.
- **Delete removes the Keycloak identity only.** The ITMS row is marked
  `status='deleted'` and kept, because 89 foreign keys across 21 tables point
  at `users.id` and a hard delete would shred the audit history that makes the
  log evidence. The row authorizes nothing from that moment on.

---

## The email-domain allowlist (`ITS_CONSOLE_EMAIL_DOMAIN`)

Refuses any token whose `email` claim is not at your domain. Unset — which is
the shipped default — means **no restriction**, exactly today's behaviour.

Allowed iff the domain equals the configured base or ends with `.` + the base,
both lowercased. `you@toeshee.com` and `you@mail.toeshee.com` pass;
`you@eviltoeshee.com` fails (no dot) and `you@toeshee.com.evil.com` fails
(wrong suffix). An address with more than one `@` is refused outright rather
than parsed. A token with no `email` claim fails closed.

### ⚠ Turning this on can lock YOU out. Read this whole section first.

The rule is applied at **all three doors** that turn a token into a principal.
There is no door it does not cover, and no door that exempts an administrator:

| Door | Refusal |
|---|---|
| HTTP API | `403` (the token is genuine; the identity is not permitted — a `401` would send the browser round the sign-in loop again) |
| Console WebSocket | close code `4403`, deliberately distinct from `4401` "bad token" |
| Remote-control tunnel | close code `4403` |

**Including your own account.** Your `org-admin` gives you no exemption; the
gate runs before any permission is consulted. And there is no in-console
recovery, because the console is the thing that has just stopped admitting
you.

### 1. Run the preflight FIRST — this is a step, not a suggestion

It asks the database who is currently on the wrong side of the rule, which is
the only way to learn the cost before paying it. Run it from the repo root:

```sh
ITS_DATABASE_ADMIN_URL="postgresql+asyncpg://its_admin:<POSTGRES_ADMIN_PASSWORD>@localhost:5432/itsuite" \
  backend/.venv/bin/python scripts/preflight_email_domain.py toeshee.com
```

| Exit code | Meaning |
|---|---|
| `0` | no **active** user would be refused — the change is safe to make |
| `1` | at least one active user would be refused; their addresses are listed |
| `2` | it could not run (no admin URL, no domain, connection failed) — this is **not** an all-clear |

It reads every `users` row in every organization through the BYPASSRLS admin
role, because "who, anywhere in this deployment, is about to lose access" has
no single tenant to scope to. It calls the same `is_allowed_domain` the three
doors call, imported rather than re-expressed as a SQL `LIKE` — a preflight
that reimplements the predicate would disagree with the gate in the direction
that matters.

**Exit `1` is the answer, not an error.** What clears it depends on which of
two situations each listed account is in. Since migration `0066` (design §7.2)
`app/api/deps.py` refreshes `users.email` from the token claim on every
admission and audits the change as `auth.email_changed`, so **signing the user
in once is what clears it** — no hand-written `UPDATE` any more.

| The account | What actually clears it |
|---|---|
| should keep access | Fix the address in Keycloak, then have them sign in once. That satisfies the **gate** immediately (it reads the live claim) and updates the **stored row** the preflight reads, so the next run stops listing them. Until they sign in, the preflight keeps reporting the old address — it can only see rows. |
| should not keep access | Disable them: console → **Users → Disable**, i.e. `POST /api/v1/users/{id}/disable`. The preflight lists non-active rows but they do **not** set the exit code, and the same gate refuses a disabled user regardless of domain |

Re-run until it exits `0`. If it still exits `1` on an address you have
already fixed in Keycloak, that is the divergence in §4 below, not a failed
fix.

### 2. Then set it, and restart

```
ITS_CONSOLE_EMAIL_DOMAIN=toeshee.com
```

```sh
docker compose up -d api gateway worker
```

Settings are read **once, at `create_app`**. Editing `.env` with nothing
restarted changes nothing, and the api you are talking to is still running the
old value — which is the trap: the change looks like it did not take, so the
natural next move is to restart, at which point the lockout arrives without
warning.

### 3. Break-glass, if you locked yourself out anyway

**Unset the variable and restart the api.** Nothing else is required, no
database change is involved, and the recovery is complete the moment the
process comes back:

```sh
sed -i 's/^ITS_CONSOLE_EMAIL_DOMAIN=.*/ITS_CONSOLE_EMAIL_DOMAIN=/' .env
docker compose up -d api gateway worker
```

Confirm you are back in before doing anything else:

```sh
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  https://itms.toeshee.com/api/v1/devices
```

`200` is recovered; a further `403` means something other than the allowlist is
refusing you.

Blank, not deleted: leaving the key present and empty keeps it visible in
`.env` next to this section. That is safe, but **not** because Pydantic reads a
blank line as unset — it does not. pydantic-settings yields the empty string,
so `console_email_domain` becomes `""`, not `None`. What makes it safe is
`domain_refusal` (`backend/app/core/identity/email_domain.py`), which strips
the configured base and treats an empty or whitespace-only one as **no
restriction** — the same branch that absorbs a stray trailing newline from a
copied `.env` line, which would otherwise be a non-empty base matching no
address at all, i.e. a refusal of everyone. Read as a restriction, `""` would
lock out the entire deployment; unrestricted is the recoverable direction.

### 4. What this does NOT achieve

- **It is not a control over what Entra will authenticate.** The rule keys on
  the `email` claim, which is asserted by the IdP. It restricts which
  identities *this platform* admits; anyone who can make your IdP assert an
  address at your domain still passes it.
- **The preflight reports the moment it runs.** An account created between the
  preflight and the restart is not in its answer.
- **The preflight and the gate read different sources, and still lag.** The
  gate tests the `email` **claim** in the live token. The preflight reads the
  **stored** `users.email` row. Since migration `0066` (§7.2) that row is
  refreshed from the claim on every admission, so the two converge — but only
  when the user signs in. The window has not closed, it has narrowed to
  "since their last sign-in": an address changed in Keycloak this morning by
  somebody who has not logged in since is still the old one here, in both
  directions. A row that reads clean can belong to someone whose live claim now
  fails, and a row that reads dirty can belong to someone the gate would now
  admit. Treat exit `0` as evidence about the rows as of each user's last
  sign-in, not a proof about the tokens they will present next.
- **It is not a replacement for disabling an account.** The same gate refuses
  any user whose ITMS `status` is not `active`, and that is the control for
  "this person has left" — a domain rule that happens to exclude them is a
  coincidence, not a policy.

## AI connections (MCP)

An AI app — Claude, or any client that speaks the Model Context Protocol —
can use ITMS **as the person who connected it**: it sees what that person
sees and can do what their role allows, every call is in the audit log, and
the app's own settings decide which tools need the person's approval. The
server is `/mcp` on the `api` container; there is no new service. Design:
`docs/superpowers/specs/2026-10-09-mcp-server-design.md`.

**It is off by default, four times over.** No AI app connects until all
four hold:

1. the deployment has a public address (`ITS_PUBLIC_CONSOLE_URL`) — without
   it `/mcp` answers 503;
2. the operator has opted in to AI apps registering themselves
   (`ITS_MCP_REGISTRATION=open` in `.env`) — without it the identity
   provider refuses every app's registration, so no app can get as far as a
   sign-in page, whatever else is set;
3. an org-admin has turned on **Settings → AI connections → Allow AI apps to
   connect over MCP** for the organization;
4. the person holds the `mcp.use` permission.

The third and fourth are checked on every call, not only at connection time.
The first two are deployment settings read when keycloak-init runs. Closing
registration again stops NEW apps registering; an app that registered while
it was open keeps its registration, and is stopped by the third and fourth
or by revoking it (below).

### Turning it on

1. **`ITS_PUBLIC_CONSOLE_URL` must be set in `.env`** to the https address
   people open the console at, with no path (for this deployment,
   `https://itms.toeshee.com`). It may already be: Email intake and the
   install link use the same setting. Check with:

   ```sh
   grep '^ITS_PUBLIC_CONSOLE_URL=' .env
   ```

   If that prints nothing or an empty value, edit the line in `.env` by hand
   so it reads `ITS_PUBLIC_CONSOLE_URL=` followed by your address. Keep a
   single line: a second `ITS_PUBLIC_CONSOLE_URL=` further down silently wins.
   The MCP address is this value plus `/mcp`, and it must match byte for byte
   what the identity provider writes into tokens, so do not add a trailing
   path.
2. **Opt in to AI-app registration.** Read "Residual risks, stated plainly"
   below first: they are the price of this line, and most of them exist only
   while it is set. From the directory holding `docker-compose.yml` and
   `.env`, this removes any earlier `ITS_MCP_REGISTRATION` line and adds the
   one that opens registration:

   ```sh
   sed -i '/^ITS_MCP_REGISTRATION=/d' .env && echo 'ITS_MCP_REGISTRATION=open' >> .env
   ```

   Only the exact word `open` opens it; any other value, or no line at all,
   keeps it closed.
3. **Deploy** as in "Upgrading an existing deployment", then run the identity
   init job on its own so its output is in front of you:

   ```sh
   docker compose up keycloak-init
   ```

4. **Check it converged.** keycloak-init sets up everything MCP needs in the
   realm on every run, and opens app registration only when the address is
   set and you opted in:

   ```sh
   docker compose logs --tail=20 keycloak-init | grep 'mcp converged'
   ```

   The first run after opting in lists what it changed, ending with
   `'registration:removed:trusted-hosts'`. Every run after that prints
   `identity init: mcp converged=nothing`. If you instead see
   `anonymous client registration stays closed (Trusted Hosts kept) until …`,
   the rest of that line names the setting the job did not see —
   `ITS_PUBLIC_CONSOLE_URL is set` (step 1), `ITS_MCP_REGISTRATION=open`
   (step 2), or both: check that step and that you ran the job from
   `deploy/`.
5. **Settings → AI connections:** an org-admin turns on **Allow AI apps to
   connect over MCP**. The tab shows the connection address.
6. **Settings → Roles:** grant `mcp.use` to the roles that may connect.
   Org-admins hold `mcp.use` by default; nobody else does until you grant it.
7. **Connect an AI app.** In the app (in Claude: add a custom connector),
   give it the connection address. The app registers itself, opens the ITMS
   sign-in page, the person signs in as usual (MFA included) and approves the
   consent screen, which names the app. Then, in the app's settings for that
   connector, set each tool to *Always allow* or *Needs approval*; tools that
   change something are safest on *Needs approval*.

### What keycloak-init converges

On every run, in this order, so that a run that dies part-way leaves
registration closed:

1. reserved client ids, disabled and unusable: `it-suite` (the console's own
   token audience) and the MCP address itself, so no app can register as
   either. If `ITS_OIDC_AUDIENCE` names a client the platform or Keycloak
   needs (`it-suite-web`, the two service clients, `account`, `admin-cli`,
   `realm-management` and the other built-ins), the job refuses before it
   changes anything, says so, and exits 5;
2. the `mcp` client scope with its consent-screen line, then its mappers — the MCP audience
   (`<ITS_PUBLIC_CONSOLE_URL>/mcp`), the `org` claim and `sub` — then the
   scope offered to apps as an optional scope;
3. the client profiles and policies for every app that registers itself, or
   later updates itself: PKCE S256 required, no implicit flow, no password
   grant, redirect URIs without wildcards;
4. the anonymous registration policies: consent required, scopes limited to
   `mcp`, `openid` and `profile`, no protocol mappers of the app's own, full
   scope off, at most 200 clients in the realm. The realm's *default* client
   scopes are still allowed (`allow-default-scopes`), so an app that asks for
   no scope of its own also gets the realm defaults such as `profile` and
   `email` — the person's name and email address in its token;
5. **last**, Trusted Hosts is removed, which is what opens anonymous
   registration — and only when `ITS_PUBLIC_CONSOLE_URL` is set **and**
   `ITS_MCP_REGISTRATION=open`. Otherwise Trusted Hosts stays (and is put back
   first if missing), registration stays closed, and the job's log names the
   missing setting. Steps 1–4 happen either way, so opening registration later
   is the flag and a redeploy.

### What Traefik routes

| Path | Goes to |
|---|---|
| `/mcp` | `api` — the MCP endpoint |
| `/.well-known/oauth-protected-resource`, `…/mcp` | `api` — tells an app which sign-in to use |
| `/.well-known/oauth-authorization-server…`, `/.well-known/openid-configuration…` | `api`, which answers 404 on purpose: apps look there first and move on after a 404, but give up on the console's 200 page |
| `/realms/<realm>/clients-registrations/openid-connect` and `…/openid-connect/<client-id>` | the identity provider — the **only** registration path it serves publicly |
| any other `clients-registrations` path, in any letter case, and any identity-provider path with `;`, `\`, `..` or an encoded `;` `\` `.` `/` NUL | refused with 403 at Traefik, before the identity provider sees it |
| `/realms/<realm>/.well-known/…` and the sign-in pages | the identity provider, unchanged |

Realm names must be lowercase (`[a-z0-9_-]`) for registration to be routed;
the default `it-suite` is. The identity provider's admin console is still not
on :443.

The `deploy` CI job proves the identity half of this live, through the real
Traefik and Keycloak containers (`deploy/keycloak/ci_mcp_flow.py`): the 403s
above, registration closed until the address is set — and, with the address
alone, still closed until `ITS_MCP_REGISTRATION=open` — an app's registration
and PKCE sign-in, a token whose audience is the MCP address and never
`it-suite`, the console's own token with `azp` `it-suite-web` and `typ`
`Bearer`, and `mcp converged=nothing` on a second run.

### Revoking

- **One app:** Settings → AI connections → **Revoke for me**, or, for an
  org-admin, **Revoke for everyone**, which needs both `mcp.use` and
  `orgs.manage` (an org-admin holds both). Every token the app already holds is
  refused from its next call, and its permission to sign in again is
  withdrawn in the identity provider. If the console says that second part
  could not be done for someone, revoke again: until it is, the app could
  reconnect for that person without asking. Revoking never deletes the app's
  registration, which is shared by every organization on the deployment, and
  never affects another organization.
- **Revoking is not a block-list.** It ends the sign-ins the app holds now;
  it does not stop the app coming back. A person can approve the same app
  again, and any app can register again under a new client id with any name
  it likes. To keep AI apps out, use one of the stops below.
- **One person:** take `mcp.use` away from their role. Their next call is
  refused.
- **The whole organization — the emergency stop:** Settings → AI
  connections → turn **Allow AI apps to connect over MCP** off. Every app is
  refused from its next call, whatever it registered as.
- **New apps, deployment-wide:** delete the `ITS_MCP_REGISTRATION=` line
  from `.env` and redeploy. The identity provider refuses every new
  registration again; apps already registered are unaffected, so pair it
  with one of the stops above or below.
- **The whole deployment — the deployment-wide stop:** empty
  `ITS_PUBLIC_CONSOLE_URL` and redeploy. `/mcp`
  answers 503 and the next keycloak-init run closes registration again. This
  also turns off everything else that uses the address — Email intake's
  Microsoft sign-in, and the public address the identity provider writes into
  invitation and password-reset mail — so it is a last resort, not a switch.

### What is never reachable over MCP

**Refused twice: never a tool, and refused by the API itself on the MCP
channel**, so a later change to the tool list cannot expose them by accident
(`app/mcp/exclusions.py`, `NEVER_EXPOSED`):

- stored-credential checkout;
- license keys;
- creating enrollment tokens;
- the install-link code: setting the link up and regenerating it;
- the agent's install command and deployment token (both mint an enrollment
  token);
- the backup recovery-key challenge and its proof;
- the first-run setup claim.

**Not tools:** files and streams — the install-link MSI, every CSV and XLSX
export, recordings, document contents, attachments (download and upload) —
and the in-app AI assistant's chat. These are not refused by the API a
second time, and do not need to be: **every MCP tool call can reach only its
own route.** The API refuses a tool's request unless the router would send it
to the exact route that tool was generated from (`app/mcp/guard.py`), so no
tool can be steered at a route that is not one.

Every result is also scanned for secret-looking field names, at every depth,
and one hit blocks the whole result rather than redacting it.

### Residual risks, stated plainly

**Risks 1, 2, 3, 6 and 7 come from self-registration, and exist only while
it is open** (`ITS_MCP_REGISTRATION=open`). Closing it — delete that line
from `.env` and redeploy — ends them for new apps; apps that registered while
it was open keep their registrations until an administrator deletes them in
the identity provider.

1. **http redirects are allowed to any host**, not only to the app's own
   machine. Keycloak 26.2's redirect enforcer has one switch for http, so
   allowing `http://127.0.0.1:…/callback` (which desktop AI apps use) allows
   `http://` to every host. An intercepted authorization code is bound to the
   app's PKCE verifier and cannot be redeemed without it.
2. **A registering app can make the identity provider fetch URLs it names**
   (`sector_identifier_uri`, `jwks_uri`, `request_uris`): blind requests from
   the identity provider's network, whose success or failure the app can
   observe. Keycloak 26.2 has no registration policy that refuses them. The
   follow-up is to route the identity provider's outbound requests through
   its proxy mappings so only the hosts it really needs are reachable.
3. **Device-code, CIBA and token-exchange grants are not restricted at
   registration.** Keycloak 26.2 offers no registration-time control for them.
4. **Realm names must be lowercase** for app registration to be routed (see
   the table above).
5. **Free text can hold a secret under an innocent name.** The scan blocks
   results by field *name*; a password someone typed into a documentation
   record's notes field is text like any other and reaches the app as the
   person could read it.
6. **Consent phishing.** Anyone can register an app with any display name
   and send a sign-in link; the consent screen is the only check between
   that link and the person's full authority. Org-admins should approve only
   apps they connected themselves. The consent screen says what approving
   hands over — "Act as you in ITMS: everything your role allows, including
   running actions on managed computers" (in Spanish for a Spanish sign-in
   page) — but it names the app by whatever name the app registered.
7. **The client cap can be filled by strangers.** Max Clients is 200 for the
   whole realm, the platform's own clients included, and anonymous
   registration needs no account: anyone who can reach the registration path
   can register apps until the cap refuses everybody, legitimate apps
   included. Nothing is exposed by it; it is a denial of service on
   connecting new apps. Closing registration stops it; the filler apps are
   removed in the identity provider's admin console.

Separately from those, and by design: the platform cannot tell whether the
person or the AI app approved a call — approval happens in the app. A
compromised app holds the person's full authority, bounded by their role, their
per-tool settings, the audit trail and revocation.

The rules this all answers to are **CLAUDE.md invariant 1c** (an AI connected
over MCP acts as the person who connected it), added with this feature, and
the design's §9.

## Publishing the agent to the console

Step 11c installs one agent by hand. This is how the console gets an artefact
to hand to everybody else: **Platform → Agent Packages** lists what you publish
here, and offers two things against it — a one-machine **install command**, and
since 2026-08-26 a **Managed deployment…** dialog for a GPO or Intune rollout.

**The path, in one line:** run the `release-agent` workflow → download its
draft release → **Agent Packages → Publish a release…** → **Mark as stable…**
→ **Update rollouts**. Steps 1–4 below. Until 2026-09-23 publishing was only
possible with `curl` and a hand-copied bearer token; that path is kept, short,
as the fallback in step 2.

⚠ **UNVERIFIED as a whole.** Unlike "First boot" above, no artefact has been
published to a running install and no endpoint has been enrolled from a
console-generated command (`docs/modules/agent-distribution/spec.md`, opening
note). The routes, permissions and refusals are covered by tests against a real
Postgres; the presigned GET as *this* deployment configures MinIO is not. Treat
the first publish as a test.

Nothing about this is automatic. The platform never builds, fetches, or pushes
an agent — it stores bytes you upload and serves them back over a short-lived
presigned link. Publishing is a deliberate act by a holder of
`agent.release.manage`.

**That code lives on `super-admin`, not on `org-admin`.** Migration `0060`
seeded it to `org-admin`; migration `0070` moved it, because the release
catalogue is global rather than per-tenant — whoever publishes replaces the
binary that every tenant's endpoints execute, and that is not an authority an
organisation administrator should hold over other organisations. `0070` revokes
it from every role by name-independent means, so a cloned `org-admin` does not
keep it either.

`super-admin` is not grantable from any console screen, deliberately. If your
everyday account does not hold it, Agent Packages shows it no **Publish a
release…** button and the `curl` fallback fails with `403 requires
agent.release.manage`. The fix is a direct grant on the database host, from
the directory holding `docker-compose.yml` and `.env`. First, by hand, set
`PUBLISHER` to the email that account signs in with — for example
`PUBLISHER=you@toeshee.com` — then:

```sh
: "${PUBLISHER:?set PUBLISHER to the email the account signs in with first}"
docker compose exec -T -e PGPASSWORD="$(grep '^POSTGRES_ADMIN_PASSWORD=' .env | cut -d= -f2-)" \
  postgres psql -U its_admin -d itsuite -v publisher="$PUBLISHER" <<'SQL'
INSERT INTO user_roles (id, org_id, user_id, role_id, device_group_id)
SELECT gen_random_uuid(), u.org_id, u.id, r.id, NULL
FROM users u, roles r
WHERE u.email = :'publisher' AND r.name = 'super-admin' AND r.org_id IS NULL
ON CONFLICT DO NOTHING;
SQL
```

```
INSERT 0 1
```

**Three things in that statement are load-bearing and an earlier version of
this runbook got each of them wrong**, which is worth saying because all three
fail *after* you have built and are ready to upload:

- **The database is `itsuite`, not `its_suite`.** `psql: FATAL: database
  "its_suite" does not exist`. It is `itsuite` everywhere else in this file and
  in `ITS_DATABASE_URL`, and it is what `docker-compose.yml` passes as
  `POSTGRES_DB`. (`PGPASSWORD` is read from `.env` rather than typed — the
  socket may or may not be `trust` depending on your image's `pg_hba`, and a
  command that works either way is one fewer thing to discover mid-upgrade.
  The email travels as a `psql` variable, `:'publisher'`, which `psql` quotes,
  so an address is never spliced into the SQL by the shell.)
- **`id` has no server default.** `user_roles.id` is a uuid7 generated by the
  *application* (`app/models/base.py::uuid7_pk`), so an `INSERT` that omits it
  dies with `null value in column "id" violates not-null constraint`. Raw SQL
  has to supply one; `gen_random_uuid()` is what migration `0003` uses for the
  same reason and is built into Postgres 13+.
- **`org_id` is `NOT NULL`.** The *role* is org-wide (`roles.org_id IS NULL`,
  which is what the `WHERE` clause matches); the *grant* still belongs to a
  tenant, so it takes the user's own `org_id`. `SELECT ..., NULL` for that
  column is `null value in column "org_id" violates not-null constraint`.

`ON CONFLICT DO NOTHING` is now belt and braces rather than the whole guard:
migration `0073` added a partial unique index on `(user_id, role_id) WHERE
device_group_id IS NULL`, so a second run is refused at the database rather
than producing the duplicated role the console used to render. Keep the clause
anyway — it turns that refusal into `INSERT 0 0` instead of an error.

Sign out and back in afterwards — the permission set is resolved at token issue.

**Do this after first boot, and again after every agent build you intend to
roll out.** It is not part of the upgrade sequence below: upgrading the control
plane does not republish agents, and an agent in the field keeps running the
version it has.

### 1. Build and sign: run the `release-agent` workflow

The binaries you publish come from `.github/workflows/release-agent.yml`, not
from a workstation. It is the only place the offline release-signing key
exists (the GitHub secret `AGENT_RELEASE_SIGNING_KEY`; the control plane never
holds it — `agent/packaging/README.md`, "The offline release-signing key"), so
it is the only build that produces release signatures, and it compiles the
public half into every binary: an agent it built refuses any update that does
not carry one. It refuses to run without the secret rather than produce an
unsigned release.

The version must be strict semver with no leading `v` — `1.4.0` or
`1.4.0-rc.1`, never a git SHA — because agents only self-update between
versions they can order. The workflow refuses anything else, and so does the
console.

**Set the version once, by hand; every block below reads it.** Type the
assignment yourself for the version you are releasing — `VERSION=1.4.0`, for
example. It is not in a block on purpose: pasted, an example version is a real
release. Each block starts by refusing to run without it.

Then, from a clone of this repository with `gh` signed in:

```sh
: "${VERSION:?set VERSION to the version you are releasing first, e.g. VERSION=1.4.0}"
gh workflow run release-agent.yml --ref main -f version="$VERSION"
gh run watch
```

`gh run watch` asks which run to follow; pick the `release-agent` one.

(Or in GitHub: **Actions → release-agent → Run workflow**, branch `main`,
the version in the box. Pushing a tag `agent-v1.4.0` does the same.) If the
`agent-release` environment has required reviewers, the run waits for one of
them to approve it.

The result is a **draft** GitHub release, `agent-v$VERSION`, with nine assets:
six binaries (`its-agent-{linux,darwin}-{amd64,arm64}`,
`its-agent-windows-{amd64,arm64}.exe`), `SIGNATURES`, `SHA256SUMS` and
`release-signing-key.pub`. Download them to the machine you run the console
from:

```sh
: "${VERSION:?set VERSION to the version you are releasing first, e.g. VERSION=1.4.0}"
gh release download "agent-v$VERSION" --dir "its-agent-$VERSION"
ls "its-agent-$VERSION"
```

(Or in GitHub: **Releases → the draft "its-agent 1.4.0" → Assets**, one click
each.) `SIGNATURES` is the file that matters besides the binaries: one line per
binary, `<sha256>  <file>  <os>/<arch>  <signature>`, each signature covering
the version, the platform and the digest together, so a signed binary cannot
be relabelled as another version or platform.

The Windows binaries are not Authenticode-signed and the macOS ones carry only
an ad-hoc signature (`agent/packaging/README.md`, "Code signing"). That is a
different signature from the release signature above, and the console keeps
the two apart.

### 2. Publish them from the console

**Platform → Agent Packages → Publish a release…** — the button is shown only
to an account holding `agent.release.manage`; everyone else sees none, and the
server refuses them regardless.

1. **Version**: exactly the `VERSION` the workflow built. The release
   signatures cover the version number, so publishing `1.4.0`'s binaries as
   `1.4.1` produces packages every agent refuses to update to — and the
   console cannot tell, because it does not hold the key that would check.
2. **Choose binaries…**: select every file in `its-agent-$VERSION` at once.
   The platform is read from each file name. `SIGNATURES` is recognised and
   read; `SHA256SUMS` and `release-signing-key.pub` are left out and the dialog
   says so. A file whose name it does not recognise (a browser's `(1)` suffix
   on a second download, say) gets two drop-downs to name its platform.
   (**Choose signatures file…** does the same for `SIGNATURES` alone.)
3. **Publish.** For each binary in turn it creates the catalogue entry, uploads
   the bytes, and then — only if the `SIGNATURES` line for that platform has
   the **same sha256 the server computed over the upload** — records the
   release signature.

Each file ends in one of these:

- **Published, release signature recorded** — what you want.
- **Published without a release signature** — the signatures file had no line
  for that platform, or you chose none. Agents built by the workflow will
  refuse to update to it; installing it with an install command still works.
- **Failed: … the checksums differ** — the bytes that arrived are not the bytes
  the workflow signed: a truncated download, or a binary and a `SIGNATURES`
  from two different runs. **The signature is deliberately not sent**; the
  server would store it without checking, and the first agent to try the update
  would be the first to find out. The dialog removes that entry, so download
  both files again from the same draft and publish that one again.
- **Failed: a release already exists for this version, OS and architecture** —
  the catalogue is unique on that triple and, deliberately, **shared across
  tenants** (it carries no `org_id`: the artefact is a property of the
  platform, not of an org). The other files are still published.

If an upload fails outright, the checksums differ, or the signature cannot be
recorded, the dialog removes the entry it had just created, so trying that file
again is not a 409. If the removal itself fails the result says so, and the
`DELETE` in the fallback below clears it.

Every release is registered as **not code-signed** (`signed: false`): that
field is an Authenticode / Developer ID claim, and the workflow makes none.
Putting `true` there for a binary nobody signed would put a false word on the
screen of the operator deciding whether to trust the download.

A new release starts on the **pilot** channel.

#### Fallback: the same thing with `curl`

For a host with no browser, or for a binary you built yourself. `$TOKEN` must
be a bearer token from a session holding `super-admin` (see above) — **not**
the Step 11 admin token unless that account has been granted the role — and
the commands run from the directory holding the downloaded assets, with
`VERSION` still set. This publishes `linux/amd64`; repeat both blocks for each
platform, changing the three names on the `OS=` line.

```sh
: "${VERSION:?set VERSION to the version you are releasing first, e.g. VERSION=1.4.0}"
OS=linux ARCH=amd64 FILE=its-agent-linux-amd64
REL=$(curl -sS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"version\":\"$VERSION\",\"os\":\"$OS\",\"arch\":\"$ARCH\",\"signed\":false,\"notes\":\"\"}" \
  https://itms.toeshee.com/api/v1/agent/releases | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/octet-stream' \
  --data-binary @"$FILE" "https://itms.toeshee.com/api/v1/agent/releases/$REL/artifact"
grep "  $FILE  " SIGNATURES
```

**Compare the `sha256` in the upload's answer with the first field of the
`SIGNATURES` line by eye before going on** — that comparison is the whole of
what the console's dialog adds. Only if they are equal:

```sh
SIG=$(grep "  $FILE  " SIGNATURES | awk '{print $4}')
curl -sS -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"release_signature\":\"$SIG\"}" \
  "https://itms.toeshee.com/api/v1/agent/releases/$REL/release-signature"
```

To remove an entry (a wrong upload, or a 409 you need to clear), set `REL` to
its `id` — `curl -sS -H "Authorization: Bearer $TOKEN" https://itms.toeshee.com/api/v1/agent/releases`
lists them — and:

```sh
: "${REL:?set REL to the id of the entry to remove}"
curl -sS -X DELETE -H "Authorization: Bearer $TOKEN" "https://itms.toeshee.com/api/v1/agent/releases/$REL"
```

It answers 409 while an active update rollout still uses the release.

A binary you built yourself with `./packaging/build.sh` has no release
signature to record. It installs, and it reports `signature: not_required`;
agents built by the workflow will not update to it.

The upload cap is 128 MiB. A static agent binary is in the low tens of MB. A
**422** on create means the version reached the object key and was refused:
only `[A-Za-z0-9._-]` survives, and a slash or a space is rejected rather than
smuggled into a path.

### 3. Check the digest the console reports

The dialog has already compared each upload with `SIGNATURES`, which the
workflow wrote over the bytes it built — that is the check that the catalogue
holds the bytes that were signed. The table keeps the evidence: each row shows
the first twelve characters of the server's `sha256`, and the install and
managed-deployment dialogs show it in full. To check it against the workflow's
own list as well:

```sh
: "${VERSION:?set VERSION to the version you are releasing first, e.g. VERSION=1.4.0}"
cat "its-agent-$VERSION/SHA256SUMS"
```

Each row also says, in two separate columns, **Code signing** (Authenticode /
Developer ID: "Unsigned" for everything the workflow builds today) and
**Release signature** ("Recorded" or "Not recorded" — whether agents will
accept an update to it).

### 4. Existing agents: mark it stable, then roll it out

The channel decides how far a rollout may go. A **pilot** release can be
rolled out only to a rollout's pilot ring; a **stable** one to every ring. The
server enforces it when a ring starts or advances, not the console.

1. **Agent Packages → Release channels → Mark as stable…** on the version's
   row. The confirmation names the version, says how many packages it changes
   (every platform of that version at once — the route is per version), and
   warns if any of them has no release signature recorded. **Move back to
   pilot…** is the way back: no rollout of that version can then start or move
   on to a ring other than the pilot until it is stable again; agents that
   already updated keep it.
2. **Agent Packages → Update rollouts → New rollout…**: choose the version, a
   device group of at most 25 as the pilot ring, and whether a second ring
   takes every other device. **Preview**, then **Create and start**. You
   advance each ring yourself, and a failure in the pilot pauses the rollout.

A version still on pilot can already go to a pilot ring alone — step 2 without
the second ring — if you would rather see it on a few machines before marking
it stable. Agents listed under **Needs manual update** predate self-update:
reinstall them once with step 5 or 6, and rollouts reach them after that.

### 5. One machine: hand out an install command

In the console: **Platform → Agent Packages → Install command**. The dialog
returns a command for **one machine**, carrying a presigned download link
(15 minutes) and a **single-use** enrollment token.

That route is gated `enrollment.manage`, not `agent.release.read`, because it
mints a device identity in your tenant — it grants nothing a token-creating
admin could not already do by hand, it only makes it one paste instead of
several.

The token is a literal in the command, so it lands in the operator's shell
history. That is precisely why it is single-use and short-lived.

**This is the only path on which the endpoint verifies the artefact.** The
generated command runs `sha256sum -c` on the downloaded file immediately before
installing it. Step 6 does not, and cannot.

### 6. A fleet: managed deployment (GPO / Intune)

**Do not paste per-machine commands for a rollout.** Since 2026-08-26 the
console serves the two halves a managed deployment actually consumes, under
**two different permissions**, and the split is the design rather than
bookkeeping — see `docs/modules/agent-distribution/spec-managed-distribution.md`.

In the console: **Platform → Agent Packages → Managed deployment…**, then pick
`GPO`, `Intune` or `Manual`. Against the API, the same two calls:

**a. The package — artefact, digest, staging steps. Carries no credential.**

```sh
curl -sS -H "Authorization: Bearer $TOKEN" \
  "https://itms.toeshee.com/api/v1/agent/releases/$REL/deployment-package?channel=gpo"
```

Gated **`agent.release.read`** — the same code that lists the catalogue,
because this response is inert. It returns the release metadata, the SHA-256,
a 15-minute presigned download URL, the per-channel staging steps, a silent
install invocation, a separate enrolment invocation, and the integrity
statement below. It **mints nothing and writes no `enrollment_tokens` row**;
that absence is a test (`test_agent_managed_deployment.py`), because a GPO
distribution point is read by every machine account in the domain and a package
carrying a token *is* a permanent fleet-wide enrolment secret sitting on a
share. Audited as `agent.deployment_package_generated`.

A **409 `this release has no artifact yet; upload one before building a
deployment package`** means a release row exists with no bytes: the `curl`
fallback's first call in step 2 ran and its second did not. (The console's
dialog removes such a row itself when an upload fails.) Staging an object that does not
exist fails at the far end of a copy, on somebody's file share, which is the
most expensive place to diagnose it — so it is refused here instead. A **422**
means the `channel` was not one of `gpo`, `intune`, `manual`; it is refused
rather than defaulted, because the channel decides the integrity statement
below and an unknown one falling back to `manual` would tell a GPO
administrator that the endpoint verifies before installing, which on their path
is false.

**b. The credential — on its own, with mandatory bounds.**

```sh
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"max_uses":260,"expires_in_hours":72,"channel":"gpo","note":"HQ rollout"}' \
  "https://itms.toeshee.com/api/v1/agent/releases/$REL/deployment-token"
```

Gated **`enrollment.manage`**. `max_uses` (1…1000) and `expires_in_hours`
(1…336, i.e. fourteen days) are **both required and neither is defaulted** —
the two numbers *are* the blast radius, and a default would be the platform
choosing it for you. The token is returned once, with the blast-radius text the
console must show. Audited as `agent.deployment_token_generated` alongside the
ordinary `enrollment.token_created`.

**Size `max_uses` to the machines you are actually deploying to.** The use
count on the Enrollment page is the detection mechanism, and 1000 uses for a
260-machine rollout hides 740 enrolments in the slack.

> **Mint fleet tokens here, not on the ordinary Enrollment screen** — but the
> reason is smaller than it was. `POST /api/v1/enrollment-tokens` (note the
> hyphen; the specs write it `/enrollment/tokens` and the route never had that
> shape) used to default `max_uses` to `null`, and `null` means **unlimited**.
> That was closed on 2026-08-26: `max_uses` is now required and capped there
> too, and no route can express the unbounded case. **The column is still
> nullable, so tokens issued before that change are still out there** — check
> the use count and the `max_uses` of anything you did not size yourself, and
> revoke what you cannot account for. What this route adds over the general one
> is the tighter fourteen-day expiry ceiling and the blast-radius text.

Two steps on the endpoint, not one, and that is the whole shape of the managed
path: the package installs, your own secret mechanism enrols.

```powershell
# staged on \\<dfs-or-sysvol>\its-agent\ , run by a computer startup script (SYSTEM)
.\install.ps1 -Binary .\its-agent-windows-amd64.exe -NoStart

$env:ITS_AGENT_ENROLL_TOKEN = <read from a location only machine accounts can read>
.\its-agent-enroll.ps1 -Url https://itms.toeshee.com
```

`install.ps1` registers the service and does **not** start it when there is
nothing to enrol with; `its-agent-enroll.ps1` (new on 2026-08-26 — Windows had
no counterpart to the Unix `its-agent-enroll`) takes the token from the
environment or stdin, **refuses `-Token`**, and is a no-op on an endpoint that
already has an identity, which matters because a startup script runs at every
boot.

**There is no MSI, so this is a computer startup script and not GPO's
*Software Installation* policy** — that policy takes an MSI and nothing else.
A startup script is a supported and widely used mechanism, with different
diagnostics. `spec.md` §3 and `spec-managed-distribution.md` §5.3.

#### What the digest comparison becomes on this path — say this out loud

The one-machine command in step 5 verifies the digest **on the endpoint**,
immediately before installing. **GPO and Intune verify nothing.** They copy a
file from a distribution point and run it. So the single comparison becomes
three checkpoints, **two of them yours and the third nobody's**:

| When | Who checks | What it establishes |
|---|---|---|
| **Publish** | the publish dialog, against the workflow's `SIGNATURES` (steps 2–3) | the catalogue holds the bytes the workflow built and signed |
| **Stage** | **you, on the file you downloaded, before it reaches the share** | the file the domain is about to run is the file the catalogue holds |
| **Install** | **nobody** | — |

The console hands you the middle command (`Get-FileHash` on Windows,
`sha256sum -c` elsewhere). Run it on your own workstation, on the file you just
downloaded, **before** it touches the distribution point — not after.

> The integrity guarantee moves from *the endpoint verifies before installing*
> to *the administrator verified once, and the distribution point is
> write-restricted*.

**And because the binary is unsigned, the share's ACL is not one control among
several — it is the only one.** There is no Authenticode signature to fall back
on (`agent/packaging/README.md`, "What is still unsigned"). Anyone who can
write to that share replaces a binary that a GPO startup script then runs as
**SYSTEM on every machine in scope**, and neither this platform nor Windows
will notice. Restrict write access to that share to the administrators who
manage it, and audit it like a domain controller, because in effect it is one.

That reduction is caused by the deployment mechanism rather than by anything in
this platform, and the only thing that would restore it is Authenticode
signing. What that would take, where the key lives, and what it does *not* fix
are written up in
[`docs/modules/agent-distribution/authenticode-signing.md`](../docs/modules/agent-distribution/authenticode-signing.md).
It is the top open item in `spec.md` §8 and it is a procurement decision, not
an engineering one.

**What leaks if the token leaks**, per channel, because it differs by an order
of magnitude and the console says so on screen when you mint one:

| Channel | Who can read the token |
|---|---|
| GPO startup script / SYSVOL file | any local administrator of any machine in the deployment (startup scripts run as SYSTEM) |
| Intune script or Win32 app | local administrator on an assigned device, or an Intune administrator (the payload is encrypted at rest) |
| Manual / configuration management | whatever your secret store grants |

A holder can enrol up to the remaining uses into your organization until the
token expires. Each is a real device from the gateway's point of view: it
appears in the fleet list and submits inventory the console will believe, and
**a rogue device is a device somebody may later target** — any job whose signed
parameters carry a secret delivers that secret to whatever answers as that
device. It cannot forge a job, dispatch anything, read another device's data,
promote itself to a site server, or mint another token. Revoking the token on
the Enrollment page kills every remaining use at once.
`spec-managed-distribution.md` §3.4 does the accounting in full.

### What publishing does not do

Uploading a Windows or macOS artefact does not mean anyone has run it. Neither
the Windows service nor the macOS daemon has ever been started by anybody on
this project (`agent/packaging/README.md`, "What is untested"), and the console
marks those rows **never executed** for that reason. Publishing them is
reasonable; treating the first install as a routine rollout rather than a test
is not — and managed distribution raises the stakes, because the first Windows
install is now potentially three hundred of them at once.

**There is now a procedure for retiring that marker by evidence.**
[`docs/modules/agent-distribution/first-run-verification.md`](../docs/modules/agent-distribution/first-run-verification.md)
is the exact sequence to execute each path once on a real machine, with what to
observe at every step and what each observation would falsify. Run it on one
Windows endpoint and one Mac before any rollout; it takes under an hour per
platform and it is what turns "structurally tested" into "executed".

---

## Upgrading an existing deployment

**The short version: `./scripts/deploy.sh`.** It pulls, diffs what arrived
against what the *running image* was built from, and runs only the steps that
change actually needs — building `web` alone for a frontend release, running
`migrate` only when a new revision file appeared, and so on. Run
`./scripts/deploy.sh --plan` first to see the decision without acting on it.

Two things it does that the manual sequence below cannot:

- **It takes the older of (running image tag, pre-pull HEAD) as its baseline.**
  On 2026-08-27 the console served new frontend code while `api` still ran an
  image built eight commits earlier, so a route that existed in the source
  answered `405`. A diff taken from the checkout would have missed it; a diff
  taken from the running image does not.
- **It verifies the deployed tag equals HEAD afterwards**, which is the check
  that incident needed and no step below performs.

Every uncertainty — an unrecognised tag, a force-push, no `IT_SUITE_VERSION` —
resolves to a *full* deploy rather than a partial one. It never edits `.env`
beyond `IT_SUITE_VERSION`, never stores key shares, and never publishes an
agent release; each of those is reported for you to do.

Since 2026-09-23 it also:

- **stops before the pull if OpenBao is sealed or down**, with the unseal
  commands, instead of letting `up -d` block; if the deploy itself recreates
  OpenBao and it comes back sealed, an interactive run asks for the shares
  at the prompt and `--yes` stops with the fix;
- **runs `keycloak-init` on every deploy**, which converges the realm
  allowlist in [Realm drift](#realm-drift-what-reaches-an-existing-host-and-what-never-does);
- **fails on any one-shot** (`migrate`, `postgres-grants`, `openbao-init`,
  `keycloak-init`) that exits non-zero;
- **reports the nightly backup timer**, or says how to install it
  ([Platform backups](#platform-backups-nightly-off-host-and-a-monthly-drill)).

The manual sequence stays below, because a script that decides for you is only
trustworthy if you can see what it decided and do it yourself.

**Order matters**, and no step here destroys a volume — never use `-v` on a
live host.

```sh
cd /opt/it-suite

# 1. Know what you are on, and be able to go back.
#    Record BOTH the running image tag and a full backup. The tag is what a
#    rollback retags to; the backup is only needed if the upgrade moves the
#    schema.
grep IT_SUITE_VERSION deploy/.env
cd deploy && ./scripts/backup.sh ~/backups/pre-upgrade-$(date -u +%FT%H%MZ) && cd ..
#    That writes globals.sql alongside the dumps, and globals.sql CONTAINS
#    THE ROLES' SCRAM PASSWORD HASHES. It is mode 600, but the directory is
#    still a secret-bearing directory: encrypt it before it leaves the host
#    (docs/07-backup-restore.md §2.1 and §2.4) and do not park it on a share.

# 2. Pull. `.env` and `certs/` are gitignored, so neither is touched.
git pull

# 3. Build the application images from the new source, TAGGED with the new
#    commit, and SELECT that tag. Both halves matter: a bare `docker compose
#    build` leaves `:dev` (not a rollback target — it names whatever was last
#    built here), and building a tagged image without updating .env means
#    steps 4-6 keep running the OLD one, an upgrade that silently does not
#    upgrade.
cd deploy
./scripts/build-images.sh
NEW_VERSION=$(git rev-parse --short HEAD)
grep -q '^IT_SUITE_VERSION=' .env \
  && sed -i "s/^IT_SUITE_VERSION=.*/IT_SUITE_VERSION=$NEW_VERSION/" .env \
  || echo "IT_SUITE_VERSION=$NEW_VERSION" >> .env
grep IT_SUITE_VERSION .env      # confirm exactly one line, with the new SHA

# 4. Migrate BEFORE restarting the app. Old code against a new schema is
#    survivable; new code against an old schema is not.
docker compose up migrate
docker compose up postgres-grants     # idempotent; picks up any new table

# 5. Re-run OpenBao provisioning. Idempotent, and it is how a corrected PKI
#    role definition reaches an existing deployment (the role writes in
#    openbao/init.sh are deliberately unconditional).
docker compose up openbao-init

# 5b. Re-run the identity init job. Idempotent, and it is how a corrected
#     realm setting reaches an existing deployment -- a realm is imported only
#     when it does not already exist, so the realm file alone changes nothing
#     on a running install. `up -d` below starts it too; running it here means
#     a failure is visible on its own rather than buried in a wall of
#     container output.
docker compose up keycloak-init

# 6. Restart the application services.
#    THIS IS ALSO HOW CELERY BEAT PICKS UP A NEW SCHEDULE. Beat is not its own
#    container -- the `worker` service runs `celery ... worker --beat`, so the
#    beat schedule is read once, at worker start, from `app/worker.py`. A
#    release that adds a beat entry does nothing at all until the worker
#    container is RECREATED, and `up -d` recreates it only because step 3
#    changed IT_SUITE_VERSION. If you ever skip step 3, force it:
#      docker compose up -d --force-recreate worker
docker compose up -d

# 7. Verify.
docker compose ps -a --format 'table {{.Service}}\t{{.Status}}'
docker compose exec -e PGPASSWORD=<pw> postgres \
  psql -U its_admin -d itsuite -tAc 'select version_num from alembic_version;'
curl -o /dev/null -w '%{http_code}\n' https://itms.toeshee.com/api/v1/devices   # 401
#    And that the running worker really holds the new beat schedule -- this
#    reads it out of the container that is actually running, not out of the
#    source tree you pulled.
docker compose exec worker python -c \
  "from app.worker import create_celery; print('\n'.join(sorted(create_celery().conf.beat_schedule)))"
```

⚠ **PARTLY UNVERIFIED as a sequence.** Every individual command here was run
during this first boot, and steps 3–7 were run repeatedly against a live
stack as fixes landed. A true `git pull`-over-an-older-release upgrade has
still not been performed on a live host, because this is the first
deployment there is.

What *is* now verified, and by what:

- **Migrations round-trip.** CI's `migration round-trip` job runs
  `upgrade head → downgrade base → upgrade head` on every push, asserts the
  downgrade leaves no tables *and no enum types* behind, and diffs the
  resulting schema against a straight `upgrade head`. It caught a real
  defect: `0001`'s downgrade exited 0 while orphaning the `job_status` enum,
  which broke the next `upgrade head`.
- **First boot.** CI's `deploy` job boots the real compose stack, runs
  `alembic upgrade head` in the built image, runs `postgres-grants` twice to
  prove idempotency, and asserts `app_user` is RLS-constrained and correctly
  narrowed.
- **Backup and restore.** `scripts/backup.sh` and `scripts/restore.sh` were
  run end to end between two purpose-built Postgres clusters shaped like
  this one (`its_admin` as bootstrap superuser, `init-app-user.sh`'s roles
  replayed, source narrowed by `grant-app-privileges.sql`): `pg_restore`
  exit 0, zero errors, and `app_user` verified narrow afterward.
- **Image tags.** CI asserts all five application services honour
  `IT_SUITE_VERSION`, which is what makes a rollback a retag rather than a
  rebuild.

What remains unverified, precisely:

- A `git pull`-over-an-older-release upgrade **on a live host**.
- A rollback **on a live host** — the tag mechanism is CI-verified, the
  operational sequence below is not.
- Restore of the `keycloak` database specifically. `backup.sh` dumps it and
  `restore.sh` restores it, but only the `itsuite` restore was asserted
  against; a realm/user/federation check after restore has not been done.

Notes that apply to upgrades specifically:

- **If OpenBao's container is recreated it comes back sealed.** Step 6 will
  hang on the healthcheck until you unseal. This is the single most likely way
  for an upgrade to appear stuck.
- **A pre-portal deployment additionally needs `portal_user` given a LOGIN
  password by hand** — see the "End-user portal" note below.
- **A deployment initialised before `postgres-grants` existed** needs the
  manual grant block in "The app_user table-privilege gap" below.
- **A new beat entry does nothing until the `worker` container is recreated.**
  Beat runs inside `worker`, not in a container of its own — see step 6 and
  the "Device-group membership is a beat sweep" note below. `device-group-reevaluate`
  (2026-08-26) is the current example: rules that never fire, on a stack where
  every container is healthy.
- **New Python dependencies live in the image, not on the host.** `openpyxl`
  became a backend dependency on 2026-08-26 (the inventory export now emits a
  real `.xlsx` workbook rather than a `.json`, `backend/app/modules/inventory/api.py`).
  `backend/pyproject.toml` pins it `>=3.1.5,<4`; step 3's
  `./scripts/build-images.sh` is what installs it. **There is nothing to
  `pip install` on the server**, and skipping step 3 while doing step 6 gives
  you an `ImportError: No module named 'openpyxl'` from `api` at startup rather
  than at the export.
- **No new `.env` keys, no new compose services and no new published ports**
  in this release. Stated rather than omitted: a step that is missing reads as
  a step that was forgotten.
- **Rolling back is retagging, not rebuilding — see "Rolling back a
  deployment" below.** Migrations remain forward-only; `alembic downgrade`
  is not a production procedure.

## Platform backups: nightly, off-host, and a monthly drill

Install once, as root, from `deploy/`:

```sh
sudo ./scripts/install-backup-timer.sh
sudo systemctl start its-platform-backup.service
journalctl -u its-platform-backup.service --no-pager -n 50
```

| Piece | What it does |
|---|---|
| `its-platform-backup.timer` (02:30 nightly) | `scripts/scheduled-backup.sh`: `backup.sh` into `BACKUP_ROOT/daily/<UTC time>`, `latest` link, retention, optional off-host copy |
| `its-restore-drill.timer` (1st of the month) | `scripts/restore-drill.sh`: restores `latest` into the throwaway project `itms-restore-drill` and checks it |
| `/etc/it-suite/backup.conf` | the one settings file (`deploy/backup/backup.conf.example`); root, mode 600 |
| `/var/lib/it-suite/backup/` | `last-success`, `last-offsite`, `last-drill` |
| `/var/lib/it-suite/backup-status/` | `backup.json`, `offsite.json`, `drill.json`: the results ITMS reads (mounted read-only into `api` and `worker`) |

**Alerts.** Platform status shows three checks: *Nightly platform backup*
(down when the last success is older than 26 hours), *Off-host backup copy*
(when `OFFSITE` is set: failed, or older than 26 hours) and *Restore drill*
(failed, or none in 35 days, counted from the first nightly backup). A check
that stays unhealthy for three minutes is sent as `platform.probe_unhealthy`,
so it reaches a person only if the organisation has a rule for *Platform problem
detected* on a channel (**Notifications → Rules**, recipients *The channel's own
default*). Before the timer is installed the checks read "not configured", not
failed.

**Object storage and fleet data.** `OBJECTS=all` mirrors the whole MinIO
bucket into `BACKUP_ROOT/objects` (incremental; deleted objects stay).
**Once Backup → Off-site copy protects the fleet's data, set
`OBJECTS=no-fleet-backups`**: it leaves out `*/backup/*`, the endpoint backup
repositories, which are then the largest and least useful part of a platform
backup. Anything already mirrored stays until you delete
`BACKUP_ROOT/objects/it-suite/*/backup`.

**Off-host.** `OFFSITE=rsync` (ssh key, `StrictHostKeyChecking=yes`, so add
the host key first) or `OFFSITE=s3` (any S3 endpoint, via `mc`). The database
backup holds role password hashes, so it leaves encrypted to
`OFFSITE_AGE_RECIPIENT` (`apt install age`), or not at all unless
`OFFSITE_ALLOW_UNENCRYPTED=yes`. Retention on the far side is the target's
job (bucket lifecycle, Object Lock).

**The drill cannot touch the live stack:** fixed project name checked
against `docker-compose.yml`'s, `COMPOSE_*` unset, no published ports, its
own volumes, passwords generated per run. It proves both dumps restore, the
RLS policy count and `app_user` narrowing (via `restore.sh`), the schema
revision, the audit-chain linkage, and that Keycloak serves the realm from
the restored database. It does **not** cover key material: `openbao-data`,
`its-secrets`, `its-ca`, the OpenBao shares or unseal key file, `.env` and
`certs/` are still backed up by hand, **to a different place than the
database backups** (`docs/07-backup-restore.md` §1-2).

Run a drill by hand at any time: `sudo ./scripts/restore-drill.sh`
(`--keep` leaves it up to inspect; `--skip-keycloak` saves memory).

## Rolling back a deployment

One ordered path. Read it before you need it.

```sh
cd /opt/it-suite/deploy

# 1. Decide what you are going back to, and whether the schema moved.
grep IT_SUITE_VERSION .env                        # the version running now
cat ~/backups/pre-upgrade-<timestamp>/image_version   # the version to go BACK to
docker images --format '{{.Repository}}:{{.Tag}}' | grep itsuite/backend
docker compose exec -T postgres \
  psql -U its_admin -d itsuite -tAc 'select version_num from alembic_version;'
cat ~/backups/pre-upgrade-<timestamp>/alembic_version   # what the backup holds
#
#    THE TWO CASES ARE DIFFERENT ROLLBACKS. If those two revisions MATCH, the
#    bad deploy did not move the schema: do steps 2 and 5 only, skip 3 and 4,
#    and nothing is lost. If they DIFFER, the schema moved and you must do all
#    of it — the old images cannot run against the new schema.

# 2. STOP the application tier first. Do not skip this: `up -d` with old
#    images against a NEW schema runs `alembic upgrade head` from the old
#    image, which fails with "Can't locate revision identified by '<newer>'",
#    and api/gateway/worker never start because they depend on it. Restoring
#    under a running app is also how you get half-written state.
docker compose stop api gateway worker web

# 3. ONLY IF the schema moved: restore the pre-deploy backup.
#    ITS_RESTORE_FORCE=1 IS REQUIRED HERE AND IT DROPS BOTH DATABASES.
#    restore.sh refuses a non-empty target by default; in a rollback the
#    target is ALWAYS non-empty, so that guard is for a mistyped restore, not
#    for this. Read what it costs, below, before running it.
ITS_RESTORE_FORCE=1 ./scripts/restore.sh ~/backups/pre-upgrade-<timestamp>

# 4. ONLY IF the schema moved: confirm the database is back where the old
#    images expect it.
docker compose exec -T postgres \
  psql -U its_admin -d itsuite -tAc 'select version_num from alembic_version;'

# 5. Point at the previous version and start. NO rebuild, no git checkout.
#    Edit the line rather than appending: a `sed -i` on a .env that predates
#    IT_SUITE_VERSION silently matches nothing and leaves you on :dev.
grep -q '^IT_SUITE_VERSION=' .env \
  && sed -i 's/^IT_SUITE_VERSION=.*/IT_SUITE_VERSION=<previous-sha>/' .env \
  || echo 'IT_SUITE_VERSION=<previous-sha>' >> .env
grep IT_SUITE_VERSION .env                        # confirm exactly one line
docker compose up -d

# 6. Verify.
docker compose ps -a --format 'table {{.Service}}\t{{.Status}}'
curl -o /dev/null -w '%{http_code}\n' https://itms.toeshee.com/api/v1/devices   # 401
```

**If step 3 ran, finish with `docs/07-backup-restore.md` §6's post-restore
checklist** — in particular the audit-chain verification in §4. A restored
database is not verified by `compose ps` and a 401; the chain check is the
one that tells you the audit log is intact and where it was truncated.

**What step 3 costs you: every write since the backup was taken.** That is
the deliberate trade — see
`docs/superpowers/specs/2026-08-15-deploy-rollback-design.md` decision D1.
Restoring is chosen over `alembic downgrade` because the 57 `downgrade()`
bodies have never run against real data, and several drop columns; a
downgrade that half-succeeds at 3am destroys data irreversibly, while a
restore loses a known, bounded window.

**If the schema did not move, skip step 3 entirely.** Rolling the images
back is then a complete rollback and costs nothing.

**`restore.sh` re-runs `grant-app-privileges.sql` and then verifies
`app_user` is narrow**, exiting non-zero if it is not. Do not skip that
output: for a database that predates `postgres-grants` it is the step that
stops `app_user` holding INSERT/UPDATE/DELETE on `permissions` and
`role_permissions`, which no trigger protects.

**Rolling back does not un-enrol agents or invalidate certificates.** Agents
reconnect on their own; see the gateway-certificate note for the observed
reconnect behaviour.

**What a restore costs beyond ordinary rows: audit history.** `audit_events`
is hash-chained and append-only, so restoring rewinds it — every privileged
action recorded since the backup is gone, and the rollback itself is not an
audited event. If the window matters (a credential access, a policy change,
a remote session), export those rows before step 3 and attach them to the
incident record.

**`IT_SUITE_VERSION` rolls back the images, not the configuration.** Step 5
runs `docker compose up -d` against the **current** checkout's
`docker-compose.yml` and `.env`, so an old image can end up under new config
— a release that added a required env var, a service, or a mount is the case
to watch. The old `git checkout <sha> && rebuild` kept the two together at
the cost of not being a real rollback. If the release changed compose or
`.env`, check those out at the old SHA too.

**Your rollback targets are local images, and `docker system prune` deletes
them.** With the tag gone, `docker compose up -d` rebuilds from the working
tree and tags the result with the old SHA — a rollback that silently
redeploys current source under a name that says otherwise. Keep at least the
last few `itsuite/backend` and `itsuite/web` tags, and prune by name rather
than wholesale.

## Renewing the gateway certificate

365 days, no automatic rotation. Re-running the issuer *is* the renewal:

```sh
cd /opt/it-suite/deploy
sudo cat /etc/it-suite/gateway-issuer.token | docker compose run --rm --no-deps -T \
  -v "$PWD/scripts/issue-gateway-cert.py:/tmp/issue-gateway-cert.py:ro" \
  --entrypoint python gateway /tmp/issue-gateway-cert.py itms.toeshee.com
docker compose restart gateway
```

The token comes from the 0600 file on stdin, never from an argument — see step 6.
Drop the `-T` and the pipe to be prompted for it instead.

**Why this pipes and GO-LIVE.md §5's timer redirects.** They are not
inconsistent and should not be unified. A `<` redirect is opened by the shell,
so it needs the *shell's* identity to be able to read a `600 root:root` file.
The timer's `ExecStart` runs as root — systemd's default, and that unit declares
no `User=` — so the redirect is right there. You, following this runbook, are
an unprivileged operator, so it is not right here.

Verified live: a new serial is issued, the gateway restarts, and connected
agents reconnect on their own within seconds —

```
level=WARN msg="gateway disconnected; reconnecting" attempt=0 in=1.315847481s
level=WARN msg="gateway disconnected; reconnecting" attempt=1 in=1.152916045s
level=INFO msg="gateway connected" url=wss://itms.toeshee.com:8443/agent
```

so renewal is not an outage. Install the systemd timer in GO-LIVE.md §5.

---

## What was broken, and how you would recognise it

The first-boot procedure had never been executed. Running it end to end found
eleven defects, all now fixed in this repository. A twelfth (#12) was found
later, by review rather than by running anything — it is the kind that a first
boot cannot surface, because it breaks on the *endpoint* rather than on this
host. #13 and #14 came from the first deploy that reached a **browser**: both
sit on the login path, both had been present since the beginning, and neither
is visible to any server-side test — which is why step 8 carried a
"no browser session, no login" marker for as long as it did. They are listed because the symptoms are unhelpful and you will meet them
again if you pin different versions.

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | `openbao` exits 1 immediately: `OpenBao has dropped support for mlock` | `openbao/config.hcl` set `disable_mlock`, which OpenBao 2.x **rejects outright** | key removed; `cap_add: IPC_LOCK` dropped with it |
| 2 | `bao operator init` fails: `http: server gave HTTP response to HTTPS client` | the CLI defaults to `https://`; the listener is plaintext in-network | `BAO_ADDR=http://127.0.0.1:8200` on the service |
| 3 | `bao operator init` fails: `failed to persist keyring: mkdir /bao/data/core: permission denied` | the named volume was mounted at `/bao/data`, which the image does not create, so Docker made it root-owned; the server runs as uid 100 | storage path and mount moved to the image's own `/openbao/file` |
| 4 | `gateway` exits: `ctx.load_cert_chain(...)` `FileNotFoundError` | cert issuance was documented as step 9, *after* the step that starts the gateway | issuance is now step 6, before anything starts it |
| 5 | enrollment 500s; `pki/sign/agent` returns `role requires keys of type rsa` | the `agent` PKI role defaulted to `key_type=rsa`; the Go agent enrolls with Ed25519. **No device could ever enroll.** | `key_type=any` (strength is enforced server-side by `_require_strong_key`) |
| 6 | certificate rotation fails a year later, silently until then | Vault's `use_csr_common_name=true` default took the CN from the agent's deliberately-empty CSR subject; `rotate.go` requires CN == device UUID | `use_csr_common_name=false`, `use_csr_sans=false`, `allowed_uri_sans`, `max_ttl=8760h` |
| 7 | **every** route answers a bare `404`; all containers healthy | `traefik:v3.1` speaks Docker API 1.24; Engine 26+ refuses it, so Traefik discovered no containers at all | bumped to `traefik:v3.6` |
| 8 | the `org` claim never appears in a token | Keycloak 26 **silently discards** user attributes not declared in the realm's user profile — 201 Created, attribute gone | `org` declared in `realm-it-suite.json` under `components` |
| 9 | no way to federate AD/Entra or manage users | the Keycloak admin console (`/admin`) was not routed by Traefik at all | `/admin` and `/js` added to the router rule. **`/admin` has since been removed again** — a console that can reset every password in the fleet does not belong on the product's public hostname, and the answer to the original defect is ITMS's own user management plus vendor break-glass over an SSH tunnel. `/js` stayed: sign-in needs it |
| 10 | first administrator authenticates but gets `403 requires ...` on everything | JIT user provisioning assigns **no role**, and nothing granted one | step 10 added to this runbook |
| 11 | `enroll: certificate and key do not form a pair: tls: failed to find any PEM data in certificate input` | OpenBao returns PEM **without a trailing newline**; the agent concatenates `cert_pem + chain_pem`, producing `-----END CERTIFICATE----------BEGIN CERTIFICATE-----`. **No device could enroll against the production CA backend.** Tests were green because they use `LocalCA`, whose PEM is newline-terminated | one-line normalisation in `backend/app/core/ca.py` (server-side, so it repairs agents already in the field) |
| 12 | an `app.deploy` job dispatches, reaches the endpoint, and fails at the artifact download with a name-resolution error; nothing in the control plane looks wrong | MinIO had **no published port and no Traefik router**, and presigned URLs were signed against `ITS_S3_ENDPOINT` (`http://minio:9000`) — a Docker-internal name. Every grant was correctly scoped, correctly time-bounded and unreachable | a `minio` router on `:443` matching `/<bucket>/`, and a separate `ITS_S3_PUBLIC_ENDPOINT` used only for signing (required in production). See "Object storage on :443" |
| 13 | the console's **Sign in** button does nothing; devtools shows repeated 0-byte `openid-configuration` fetches and no container logs anything | `lib/auth.ts` read the OIDC authority from `VITE_OIDC_AUTHORITY`, which is set **nowhere** in this repository, so every image ever built carried the fallback `http://localhost:8080/realms/it-suite` — each browser asked *its own machine* for the discovery document. Nothing server-side can log a request that never left the laptop | the authority is derived from `window.location.origin`; Keycloak is on the same origin under `/realms`, so it cannot drift from the deployment's hostname. `frontend/.env.development` keeps the override for the dev server |
| 14 | Keycloak answers **`Invalid parameter: redirect_uri`** on a correctly-formed authorization request | the `it-suite-web` client shipped `redirectUris: ["https://*/auth/callback"]`. Keycloak honours a wildcard **only as a trailing `*`**; one in the host position is compared as a literal string and matches nothing. `rootUrl` was also `https://localhost`, so even a relative URI resolved to the wrong host | `rootUrl` emptied and the redirect made **relative** (`/auth/callback`), which resolves against the request and is therefore correct at any hostname. **A running realm is not re-imported** — an existing deployment must be patched through the admin API |

Two smaller things worth knowing:

- A failed enrollment still creates the device row before it fails, so a
  retried enrollment leaves an orphan device. Delete it from the console.
- The API's liveness endpoint is `/livez`, which is **not** under `/api` and
  is therefore not routed by Traefik — an external health check has to use a
  routed endpoint (`/api/v1/audit/verify` returning `401` proves the API is
  answering). Internally: `docker compose exec api python -c "import httpx;
  print(httpx.get('http://127.0.0.1:8000/livez').status_code)"`.

## Notes

### The app_user table-privilege gap (upgrading an existing deployment)

Before this fix, `deploy/postgres/init-app-user.sh` granted `app_user`
(the RLS-constrained role every API/gateway/worker request runs as) only
`CONNECT` on the database. The actual table privileges existed only in
`backend/tests/conftest.py`'s test harness, so a fresh deployment ran
migrations successfully and then failed on the very first real query with
`permission denied for table ...` — the test suite stayed green throughout
because it silently supplied what production never got.

**New installs** get this automatically: `init-app-user.sh` now sets up a
standing `ALTER DEFAULT PRIVILEGES` rule (step 2 above) and
`docker compose up postgres-grants` (step 4b above) applies the real,
narrowed privilege set once the schema exists. Nothing further to do.

**Existing deployments** (any database initialized before this change) will
not pick up `init-app-user.sh`'s new statements — Postgres only runs
`docker-entrypoint-initdb.d` scripts once, against a brand-new data
directory. After `docker compose up migrate` on an existing deployment,
connect as `its_admin` (or any superuser) and run:

```sql
GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
REVOKE UPDATE, DELETE ON audit_events FROM app_user;
REVOKE INSERT, UPDATE, DELETE ON permissions, role_permissions FROM app_user;
REVOKE UPDATE, DELETE ON security_events FROM app_user;

ALTER DEFAULT PRIVILEGES FOR ROLE its_admin IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES FOR ROLE its_admin IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_user;
```

(This is exactly `deploy/postgres/grant-app-privileges.sql` plus the two
`ALTER DEFAULT PRIVILEGES` statements from `init-app-user.sh` — an existing
data directory never ran that script at all, so both halves are missing,
not just the table grants. `docker compose up postgres-grants` also works
for this — it runs the same `grant-app-privileges.sql` against whatever
Postgres it's pointed at — but it does **not** set up
`ALTER DEFAULT PRIVILEGES`, so a migration added after you upgrade would
reintroduce the same `permission denied` failure for its new table[s] unless
you also run the two `ALTER DEFAULT PRIVILEGES` statements above, once, by
hand.) This step is idempotent and safe to re-run.

- The gateway entrypoint (`:8443`) is **TCP passthrough** in Traefik: the
  gateway process terminates mutual TLS itself so the agent's client
  certificate reaches it.
- OpenBao listens plaintext **inside the compose network only** and is never
  routed by Traefik.
- The audit chain can be verified any time via `GET /api/v1/audit/verify`.
- Scale-out (multiple API/gateway replicas, Postgres replication, MinIO
  distributed) is a compose/Helm topology change; see docs/01-architecture.md.

### Audit HMAC key (`its-secrets`)

- The key is **required in production** and the api/gateway **fail closed**
  without it — see first-boot step 5. It lives on the `its-secrets` named
  volume rather than a container path so it survives
  `docker compose up --force-recreate`, and so the api and gateway load
  identical bytes.
- **All three processes mount it.** `create_celery()` calls
  `audit_service.set_hmac_key(load_audit_hmac_key(settings))` as its first
  statement, so the `worker` loads the key exactly as the api and gateway do.
  It must mount `its-secrets` and it must be the *same* key: a worker holding
  different bytes would authenticate its links under a different secret, which
  fails verification identically to not keying them at all.
- **Historical caveat — pre-fix worker events cannot be repaired.** Worker
  audit writes landed before the keying did, so any deployment that ran the
  worker with a key configured between 2026-07-20 and this fix holds unkeyed
  links interleaved in a keyed chain. `audit_events` is append-only (the
  trigger rejects UPDATE/DELETE for every role, superusers included), so there
  is no migration — and re-keying in place is exactly the operator-rewritable
  history the design refuses. `GET /api/v1/audit/verify` will report those
  orgs `ok: false` permanently. See `docs/07-backup-restore.md` §4 for how to
  tell this apart from a failed restore.

### Upgrading: the postgres container needs the app role passwords

`deploy/postgres/init-app-user.sh` interpolates `APP_DB_PASSWORD` and
`PORTAL_DB_PASSWORD`, but the `postgres` service originally received neither —
so a genuinely fresh boot aborted with
`APP_DB_PASSWORD: APP_DB_PASSWORD must be set` and the container exited 1
before the database was ever usable. Both are now passed explicitly in
`docker-compose.yml` (explicitly, not via `env_file:`, so the database
container never sees the OpenBao token or the S3 keys). Nothing to do on an
existing deployment whose data directory already initialized — the init script
never runs again there — but a re-init from an empty volume needs this fix.

### Remote control (media path)

- Remote control is **screen share**
  (`docs/modules/remote-control/spec-screen-share.md`). The agent dials the
  **relay** outbound over mTLS on the gateway's existing passthrough port
  (`:8443`, path `/relay` instead of `/agent`, `ITS_GATEWAY_RELAY_URL`). The api
  joins the other end through the gateway's **internal listener**,
  `ws://gateway:8444/relay-internal` (`ITS_RELAY_INTERNAL_PORT` on `gateway`,
  `ITS_RELAY_INTERNAL_URL` on `api`, both set in `docker-compose.yml`). 8444
  is **not published** and not routed by Traefik; never add it to `ports:`.
  Nothing new is opened on the host or its firewall.
- `guacd` is **retired** and no longer in `docker-compose.yml`. The API refuses
  RDP/VNC/SSH sessions outright. A host that ran an older stack keeps a stopped
  `guacd` container until `docker compose up -d --remove-orphans` removes it;
  `ITS_REMOTE_GUACD_*` keys left in `.env` are ignored.
- Session recording **retention is app-driven, not object-storage lifecycle**:
  the daily Celery beat sweep `app.worker.apply_remote_recording_retention`
  (in the `worker` service) deletes expired recordings from MinIO/S3 and
  clears their DB refs per each org's `RemotePolicy.recording_retention_days`.
  Do not rely on (or additionally configure) a bucket lifecycle rule for
  `it-suite`'s remote-recording objects — the app is the sole source of truth
  for when a recording is deleted.

### Patch management

- Patch **scan and apply** run as **Celery-beat-driven sweeps** in the existing
  `worker` service — no new compose services required. The scan sweep (hourly)
  checks each device and dispatches a `patch.scan` job if the device's last
  scan is older than its policy `scan_interval_hours` (default 24h, resolved
  per device-group or org-default). The apply sweep (every 15 min) dispatches
  approved patches for devices whose maintenance window is currently open.
- **Approval is the apply gate**: patches must be approved for a ring before
  they are dispatched to any device in that ring. A patch is only dispatched
  if an approval exists for the device's ring and the maintenance window is
  open.
- **Compliance and SOC 2 evidence:** point-in-time exports (per-device patch
  status) are available via `GET /api/v1/patch/report/export` — same pattern
  as security-log and inventory exports. See `.env.example` for patch cadence
  documentation and the macOS Apple-Silicon MDM requirement.

### Object storage on :443

Endpoints have to be able to fetch objects. `app.deploy` hands an agent a
presigned URL for one package artifact; backup hands it a presigned upload for
one repository prefix. Both are minted by the control plane, carried inside the
**signed** job envelope, and are the only way an endpoint ever touches the
bucket — no agent holds an object-store credential (invariant #3).

Two things make that work, and they are separate:

**1. The route.** `docker-compose.yml` gives the `minio` service a Traefik
router on the existing `:443` entrypoint, matching object reads under
`/<bucket>/` and the bucket-level `POST` an upload grant targets. There is no
new hostname, no second certificate, and **no `stripPrefix`** — the prefix is
the bucket name, so `https://itms.toeshee.com/it-suite/<key>` is already
exactly what a path-style S3 URL looks like and nothing is rewritten. That is
not a stylistic choice: SigV4 signs the host and the full path, so a stripped
prefix makes MinIO recompute the signature over a different path and refuse
every grant, and a foreign prefix that is *not* stripped gets read as the
bucket name. If you ever move object storage to its own subdomain, you need a
DNS record and a certificate SAN for it, and `ITS_S3_PUBLIC_ENDPOINT` becomes
that host.

**2. The endpoint the URLs are signed against.** `ITS_S3_ENDPOINT`
(`http://minio:9000`) stays internal — bulk object traffic never leaves the
Docker network. `ITS_S3_PUBLIC_ENDPOINT` is used for signing only. It is
required in production: without it the api and worker **refuse to touch object
storage at all** rather than mint URLs naming `minio`, which no agent can
resolve. See `.env.example`, which also covers the external-S3 case.

#### What the route does and does not permit

Measured against this MinIO release behind this exact Traefik rule, with the
`web` router present — which matters, and is the reason the right-hand column
is not what you would guess:

| Request | Served by | Result |
|---|---|---|
| Presigned `GET`/`HEAD` of one object | MinIO | **200**, bytes match |
| Unsigned `GET` of a known key | MinIO | 403 `AccessDenied` |
| Unsigned `GET` of a key that does not exist | MinIO | 403 `AccessDenied` — not 404; it refuses before it looks |
| Expired presigned `GET` | MinIO | 403 `AccessDenied`, message `Request has expired` |
| Presigned `GET` with the key edited | MinIO | 403 `SignatureDoesNotMatch` |
| `POST` to `/<bucket>` with a signed policy | MinIO | 204; a key outside the signed prefix 403s, an oversized body 400s |
| `GET /<bucket>?list-type=2`, correctly signed | **the SPA** | 200 — and **no listing**: the request never reaches MinIO |
| `GET /<bucket>/`, `GET /<bucket>` | **the SPA** | 200, same reason |
| Presigned `PUT` / `DELETE`, correctly signed | **the SPA** | 200 — the object is untouched; the key a `PUT` names is never created |
| `/`, `/minio/health/live` (console paths) | **the SPA** | 200 — never a MinIO console |

**Do not read those `200`s as "it worked".** The object route claims exactly
what it claims; everything else on `:443` falls through to the frontend's
catch-all (`PathPrefix(/)`, priority 1), which answers `index.html` for any
path it is handed. So the security property is "MinIO never saw the request",
not "the client got an error" — verified by the object still being present
after a signed `DELETE` through the route, and by the key a signed `PUT` named
never being created. A bare Traefik with no `web` service answers 404 to all
of these instead, which is why an isolated test of this rule reports different
codes from a real stack.

Three rows deserve the reasoning rather than just the result:

- **Anonymous access is denied by default and you must not change that.** A
  bucket created by the application has *no* bucket policy at all
  (`NoSuchBucketPolicy`), and MinIO denies unauthenticated access to a bucket
  with no policy. Never run `mc anonymous set download` on this bucket: every
  read this platform performs is presigned, so a public-read policy grants
  nothing to the platform and everything to everyone else.
- **`PUT` and `DELETE` are unroutable**, so even a leaked MinIO root credential
  cannot delete a tenant's backups through `:443` — a correctly signed
  `DELETE` gets the SPA and the object is still there afterwards. The control
  plane's own deletes go over the internal endpoint and are unaffected.
- **An expired URL grants nothing.** Expiry is checked when the request is
  received, so a download that *starts* one second before expiry completes
  however long it takes — but a request that arrives after it is refused
  outright, and the URL cannot be renewed by the holder.

The one thing the route cannot distinguish is a correctly signed request made
by someone who should not have the credential: a leaked `ITS_S3_SECRET_KEY`
would allow listing (which this platform never signs) as well as reads. That
key is an OpenBao-free `.env` value on this host, so treat it with the same
care as the database passwords — see the go-live checklist.

#### Verify the route after first boot

```sh
# From a machine that is NOT this server, with an object you know exists.
docker compose exec -T api python - <<'PY'
from app.core.config import get_settings
from app.core.storage import resolve_presign_endpoint
s = get_settings()
print("environment:  ", s.environment)
print("internal:     ", s.s3_endpoint)
print("signs against:", resolve_presign_endpoint(s))
PY
```

The signing endpoint must be your `:443` hostname. If it prints
`http://minio:9000` you are not running `ITS_ENVIRONMENT=production` *and*
`ITS_S3_PUBLIC_ENDPOINT` is unset — fix both, because every artifact URL that
deployment mints is unusable in the field. In production the same command
raises `PublicEndpointError` naming the setting instead of printing anything,
which is the point: the api and worker will not touch object storage at all in
that state.

Then confirm the route from a machine that is **not** this server, using the
go-live checks in `GO-LIVE.md` §3 — a `403` (not a `404`) on an unsigned
object GET is what "routed, and refusing" looks like.

### Application deployment

- Application deployment (install/update/uninstall packages and signed custom
  scripts) is **RBAC-gated and operator-initiated** — there is no approval-ring
  workflow like patch management. Operators with `deploy.create` / `deploy.execute`
  permissions can target deployments at devices or groups and schedule them
  within a maintenance window or deploy immediately.
- Scheduled deployments run via the **Celery-beat `deploy-window-sweep`** (every
  15 minutes) in the existing `worker` service — no new compose services required.
- **Artifact storage and integrity:** package installers are stored in object
  storage (MinIO) and referenced by `artifact_ref` in the packages table. Manager-sourced
  installers (winget/choco/brew) have no artifact. The agent verifies the artifact
  SHA256 before execution; a mismatch fails the job.
- **Custom scripts:** script bodies travel inside the Ed25519-signed job envelope,
  so the agent verifies the signature before running as SYSTEM/root. There is no
  inline or unsigned script path. Every script execution is audited with the script
  version and actor.
- **Site-server cache:** a documented seam (Phase 7 provisioning) — agents will
  prefer a site-server cache when present, else fetch from object storage.
  (**Not built.** See "Site servers" below for the seam's two prerequisites —
  the invariant amendment AND the absent artefact origin-fetch path.)

### Backup engine

- **File-level backup with a pinned repository format** (v1). The agent-side
  engine is capability-gated and dormant until a `backup.run`/`backup.restore`
  job arrives; it is **not built**, and belongs under `agent/` in this
  repository. This control plane pins the wire contracts, the repository
  format, and the scheduling.
- **Per-tenant object-storage layout:** every backup object lives under the
  org's own prefix in MinIO — `{org_id}/backup/{repository_id}/…` — with
  `chunks/{id[:2]}/{id}` for content-addressed chunks and
  `manifests/{backup_point_id}` for the per-run manifest. There is **no
  cross-org chunk sharing or dedup**; capacity planning is per repository, and
  deduplication savings (content-defined chunking, store-once by chunk id)
  accrue only within a single repository. Growth is bounded by GFS retention
  plus garbage collection (below), not by object-storage lifecycle rules.
- **Envelope key management (invariant #3):** each repository has a master key
  **sealed in the vault** (OpenBao envelope encryption); it is never stored in
  the application database in plaintext, never returned in an API response, and
  never written to a log or audit event. At dispatch the server decrypts it
  only to place a job-scoped `repo_key` **inside the Ed25519-signed job
  envelope** — the only path by which it reaches an agent. Chunks and manifests
  are AES-256-GCM under that repository key; no plaintext backup data is ever at
  rest in object storage.
- **Three Celery-beat sweeps** run in the existing `worker` service — **no new
  compose services required**:
  - `backup-window-sweep` (every 15 minutes) dispatches `backup.run` for
    devices whose effective policy has an open maintenance window.
  - `backup-retention-gc` (daily) applies each device's **GFS** retention
    (grandfather-father-son: daily/weekly/monthly/yearly), marks superseded
    points `pruned`, then garbage-collects chunks no longer referenced by any
    live point in the repository.
  - `backup-verification` (daily) re-reads points and checksums their chunks
    against the manifest (BLAKE3), recording integrity (`verified_at`) and, for
    verify-mode restores, proven-restore status (`restore_tested_at`).
- **Restore & proven-restore:** operators with `backup.restore` browse a point's
  manifest (metadata only — never chunk bytes leave the server) and restore a
  selection to the **original or an alternate location**, or run a verify
  (proven-restore to a scratch target). All dispatch is RBAC-gated,
  signed-job-only, and audited.
- **Site-server LAN cache:** a documented seam (Phase 7) — LAN-heavy backup and
  restore traffic will prefer a site-server cache when present; not built in v1,
  and blocked on both prerequisites listed under "Site servers".

### Provisioning & imaging

- **Control-plane-first (v1).** The registry (`os_image` / `provision_profile`),
  answer-file rendering, and the **real agent auto-enrollment** are built here;
  the site-server (PXE/TFTP/DHCP, wake-on-LAN, LAN payload cache) is pinned as a
  contract and **not** built (see the last bullet). Three per-OS strategies under
  one workflow: Windows golden-image (WinPE/WDS + `unattend.xml`), Linux scripted
  install (kickstart/preseed/cloud-init), macOS **ADE/MDM enrollment** (Apple
  Silicon cannot be imaged) — no answer file, enrollment-records only in v1.
- **Per-tenant object-storage layout:** provisioning objects live under the org's
  own prefix in MinIO — `{org_id}/provision/…` — with image/build payloads
  (`artifact_ref`, integrity-checked by `artifact_sha256`) and per-run rendered
  answer files at `answers/{provision_run_id}`. No cross-org sharing. Provisioning
  adds **no new required env vars** and **no new compose services** — it reuses
  the existing object storage, vault, Redis, and the enrollment CA.
- **Answer-file secrets (invariant #3):** `provision_profile.post_config` stores
  domain-join / local-admin credentials **only as vault-sealed references**
  (`{"__sealed__": …}`), never plaintext. The plaintext is decrypted **only at
  render time**, inside the deterministic renderer, and injected into the answer
  file — never persisted in the DB, returned in an API response (profiles expose
  only `has_domain_join_credential` / `has_local_admin_credential` booleans),
  written to a log, or placed in an audit event. Rendered answer objects are
  **short-lived**: the timeout sweep deletes them once a run is terminal. Editing
  a profile without re-typing a password preserves the existing sealed credential.
- **Enrollment handoff (reuse, not fork):** starting a run issues a
  provisioning-scoped, single-use enrollment token (the existing token system)
  and embeds it in the rendered answer file; the freshly-imaged machine
  auto-enrolls via the real `POST /enroll/v1` mTLS flow, and the run correlates
  the new device back to itself (`awaiting_enrollment → enrolled`). A signed
  `provision.enroll` post-config job then verifies hostname/domain-join/agent
  health (`enrolled → complete`).
- **Two Celery-beat sweeps** in the existing `worker` service — **no new compose
  services**:
  - `provision-enroll-dispatch` (every 5 minutes) dispatches the signed
    `provision.enroll` post-config job for `enrolled` runs (the closing link of
    the flow; the API deliberately does not publish it).
  - `provision-timeout-sweep` (daily) fails runs stuck in `awaiting_enrollment`
    past 24h and deletes any lingering secret-bearing answer objects.
- **Site-server contract — superseded 2026-08-06; see "Site servers" below.**
  This entry described a `provision.prepare` envelope signed for a site server
  that authenticates "via mTLS scoped to caching/PXE/relay". Both halves are now
  wrong: there is no site-server certificate profile (a site server is a
  promoted `Device` with an ordinary agent certificate), and the discarded
  `provision.prepare` signature has been deleted. **Wake-on-LAN is built**, as a
  `site.wake` job. PXE/TFTP/DHCP and the LAN cache are still not built.

### Site servers

- **A site server is a promoted `Device`, not a new component.** There is
  **nothing new to deploy** — no compose service, no new container, no new
  port, no new env var. An operator promotes an already-enrolled endpoint by
  creating one `site_server` registry row (`POST /api/v1/site-servers`,
  requiring `site.manage`), declaring which LAN segments it serves. It keeps its
  ordinary agent, its ordinary device-bound mTLS certificate, and its ordinary
  outbound-only connection to the gateway.
- **What v1 actually does:** `site.wake` (a Wake-on-LAN magic packet, sent from
  the correct segment) and a sweeper preference — IPAM prefers a site server for
  a `net.sweep` of a segment it serves. The broadcast **address** is computed by
  the control plane from the declared segment containing the target and signed
  into the job's params; the agent broadcasts to that address and nothing else.
- **Nothing listens.** `site.wake` opens a UDP socket, sets `SO_BROADCAST`,
  sends up to three datagrams and closes. No bind to a service port, no accept
  loop, nothing resident. `CLAUDE.md` invariant #2 holds verbatim and was not
  amended.
- **The agent-side capability does not exist yet.** No Go code implements
  `site.wake`; what ships is the exported wire contract
  (`docs/kernel/wire-protocol/SiteWakeParams.schema.json` and
  `SiteWakeResult.schema.json`). A wake dispatched today reaches an agent that
  will refuse the job type.
- **Permissions:** `site.read`, `site.manage`, `site.wake`. `site.manage` and
  `site.wake` are deliberately separate — waking a machine inside an existing
  configuration is technician work; deciding that a host may serve an entire
  subnet is not.

#### The LAN cache seam — TWO prerequisites, not one

The cache is specified in full (`docs/modules/site-server/spec.md` §7) and
**not built**. Both of the following must be resolved first, and the second is
the larger of the two:

1. **`CLAUDE.md` invariant #2 must be amended.** It says "no agent listening
   port", and a cache is an HTTP responder. The proposed replacement — read-only
   content responders, bound only under a signed and time-bounded lease, only on
   the leased segments, serving only digests fixed by a signed job — is drafted
   in §4.4 and **has not been accepted**. It must not be adopted as a side
   effect of anything.
2. **There is no agent-facing artefact origin-fetch path in this repository at
   all.** `ObjectStore` has no presigned-URL method, there is no download route
   (only `POST /api/v1/deploy/packages/{id}/artifact` for operator upload), and
   `AppDeployParams` carries an `artifact_ref` an agent has no way to resolve.
   So a cache today would be a cache in front of nothing, and building it would
   mean inventing the origin path inside the site-server module — work that
   belongs to whoever owns artefact distribution.

When it is built, a cached artefact's integrity never depends on the cache: the
consumer verifies a `sha256` that arrived inside its **own signed job envelope**,
and the cache is not on the path that digest travelled. A poisoned cache is a
failed job, never a compromised endpoint. Nothing confidential is cacheable —
not rendered provisioning answer files (they carry vault-decrypted credentials),
not backup chunks, not session recordings, not job params.

### Change management

- **Control-plane only.** The change-management module (change requests,
  approval workflow, calendar, advisory action-linking, SOC 2 evidence export)
  is entirely server-side — **no agent capability, no Celery sweeps, no new
  compose services**. It adds **no new required env vars** beyond the ticket
  backend selector below.
- **Approval workflow with segregation of duties.** Changes move through a
  server-enforced state machine (`draft → submitted → approved → implementing →
  completed`, plus `rejected`/`cancelled`); a requester **cannot approve their
  own** non-emergency change (enforced in the state engine, not just the UI).
  Emergency changes may fast-path to `implementing` but **cannot close until a
  retrospective approval is recorded**. RBAC: `change.request` (technician) is
  separate from `change.approve` (org-admin), reinforcing the separation.
- **The audit log is the evidence.** Every change action (create, submit,
  approve/reject, transition, link/unlink, evidence export) writes a
  hash-chained audit event (`target_type="change"`); the evidence export
  resolves a change's audit references through the new
  `audit_events (org_id, target_type, target_id)` index. Viewing a change is a
  plain read — only the explicit **Export evidence** action is itself audited as
  `change.evidence_exported`.
- **Advisory action-linking (not gating).** A change records the patch runs,
  deployments, or provisioning runs that implement it (org-scoped polymorphic
  resolution — a cross-org action id is rejected). In v1 this linking is
  **advisory**: it produces the evidence trail but does **not** block those
  actions. Enforced change-gating is a pinned later-phase extension point.
- **Change-ticket adapter — the port now has a real backend, with a known
  limitation.** The module talks to a ticket system through a
  `ChangeTicketPort` (one-way push: create/update/close a ticket from a
  change). The default since 2026-09-23 is the **Service Desk** module's
  `ServiceDeskChangeTicketAdapter` (`ITS_CHANGE_TICKET_BACKEND=service_desk`),
  which opens an internal incident ticket per submitted change; set
  `ITS_CHANGE_TICKET_BACKEND=null` to opt out (no ticket, no network I/O). A
  `.env` copied from an older `.env.example` still says `null` and keeps the
  old behaviour until that line is changed; an Odoo Helpdesk adapter is
  no longer the intended target for this port (the platform now owns
  ticketing itself — see the Service Desk section below). **Opened tickets
  never auto-resolve.** Change management's reviewed core
  (`change_mgmt.service`'s `transition_change`/`decide_change`) always calls
  the port's `close_change` with an empty `external_id` — its only call site
  today — and `close_change` has no other way to identify which ticket to
  resolve, so it silently no-ops. In practice: a change ticket opens and
  accepts system comments as the change progresses, but is **never** driven
  to `resolved`, so enabling this backend grows a helpdesk queue of
  permanently-open change tickets, reconcilable only by hand. Closing this
  gap means threading a real `external_id` (or the change itself) into
  `close_change`, a change to `change_mgmt`'s reviewed core that is
  deliberately out of scope for the Service Desk module. (A related,
  separately-found detail for anyone touching this code next: the adapter's
  own terminal-status guard in `close_change` — `if ticket.status in
  FINAL_STATUSES or ticket.status == "closed": return` — makes no behavioral
  difference today; removing it produces an identical outcome, because
  every status it exists to catch is already a no-op through the if-chain
  below it. Harmless while `close_change` is inert through the pinned call
  site above, but worth fixing alongside the inertness rather than in
  isolation — see the adapter's own docstring.)

### BCP / DR test reports

- **Control-plane only.** The business-continuity / disaster-recovery module
  (DR plans, manual test recording, findings, and SOC 2 test reports) is
  entirely server-side — **no agent capability, no Celery sweeps, no new
  compose services, and no new required env vars**. It reuses the existing
  Postgres, audit log, and RBAC; nothing new to provision.
- **Reads backup evidence, does not run backups.** DR test evidence is pulled
  from the **backup** module's per-device restore-test status (metadata only —
  hostname, coverage, last backup / verified / restore-tested timestamps). BCP
  never touches backup payloads or object storage; automated restore drills
  (which would reuse the backup capability) are a pinned later-phase extension,
  not in v1.
- **A recorded test freezes a point-in-time evidence snapshot.** When a test is
  recorded, the current backup restore-test status for the plan's covered
  devices is captured into the test row as a durable JSON snapshot
  (`captured_at` / per-device rows / `summary`). That snapshot is **immutable** —
  later backup activity does not alter an already-recorded test — so it stands
  as point-in-time SOC 2 DR proof. The plan-status dashboard, by contrast, shows
  the **live** current coverage.
- **The audit log is the evidence.** Every privileged action (plan
  create/update, review sign-off, test create/record, finding create/update)
  writes a hash-chained audit event (`target_type` `bcp_plan` / `bcp_test` /
  `bcp_finding`). Viewing plans, tests, findings, or the dashboard is a plain
  read and writes nothing; only the explicit **report export**
  (`GET /bcp/tests/{id}/report`, `GET /bcp/report/period`) is itself audited as
  `bcp.report_exported`. RBAC: `bcp.read` (view/report), `bcp.manage` (author
  plans, record tests, manage findings — technician), `bcp.approve` (review
  sign-off — org-admin).
- **Review-overdue is computed on read; findings link advisorily to Change.** A
  plan is flagged overdue when `last_reviewed_at + review_interval_days` has
  passed (or it was never reviewed) — computed at report time, with **no
  notification/alerting mechanism in v1**. Findings may reference a change
  request (`change_ref`, org-scoped) as an **advisory** remediation link; it
  records the trail but does not gate anything.

### Licenses (SAM)

- **Control-plane only, plus one worker sweep.** The Licenses / software-asset-
  management module (vendors, software products, licenses, allocations,
  compliance reconciliation, reports) is server-side — **no agent capability, no
  new compose services, and no new required env vars**. It reuses Postgres, the
  vault, the audit log, and the existing Celery/Redis worker. It introduces the
  shared **`vendor`** entity that the rest of the financial trio
  (subscriptions, budget) will reuse.
- **Compliance is reconciled against real inventory.** Each `software_product`
  carries `match_rules` (exact/prefix/regex, operator-authored behind RBAC);
  installed software from the **inventory** module is matched to products, and
  compliance per product is `entitled` (Σ license entitlements) vs `installed`
  (distinct devices running a matching product) vs `delta`. Names matching no
  product surface in a **review queue** to map (→ product) or ignore. A daily
  Celery-beat sweep (`licenses-compliance-sweep`, 24h) persists
  `license_compliance_snapshot` rows for history; reports also compute live at
  read time. **Renewal/breach "alerts" are computed flags in the reports — there
  is no push/email channel in v1.**
- **License keys are sealed, and reveal is audited.** Keys are encrypted with the
  per-org vault envelope (`seal_bytes`/`open_bytes`), decrypt-at-use, never in
  list/detail responses (`has_key` boolean only) and never logged. The single
  reveal endpoint `GET /licenses/{id}/key` is gated by a distinct
  `licenses.keys.read` permission and writes a `license.key_revealed` audit on
  every access. RBAC: `licenses.read` (view/report), `licenses.manage` (author
  vendors/products/licenses/allocations, reconcile — technician), and
  `licenses.keys.read` (reveal a key — org-admin/super only). Viewing catalogs,
  licenses, or the compliance dashboard writes no audit; only the explicit CSV
  **export** (`GET /licenses/export`) is audited as `license.export`.
- **Cost is per-record; no FX in v1.** Each license stores `cost` + an ISO-4217
  `currency`; reports group and sum **by currency** (no cross-currency roll-up).
  A per-org base currency and exchange-rate conversion are owned by the Budget
  module (a later phase). Agreement attachments are an optional reference string
  in v1 (managed object-storage upload is a fast-follow).

### Subscriptions (SaaS management)

- **Control-plane only, and — unlike Licenses — no worker at all.** The
  Subscriptions module (SaaS/seat registry, renewal and cancellation-notice
  tracking, seat assignment, spend reporting) is entirely server-side: **no agent
  capability, no Celery beat sweep, no new compose services, and no new env
  vars.** It reuses Postgres, the audit log, RBAC, and the shared **`vendor`**
  entity introduced by Licenses. Nothing to configure to run it.
- **The notice cutoff is the decision point, computed at read time.** Every
  subscription carries `renews_at` and `notice_days`; the renewal calendar
  derives `notice_deadline = renews_at − notice_days` and flags
  `action_required` when the subscription auto-renews and today has reached that
  deadline — after it passes, the renewal is committed and the money is spent.
  Because these are computed on read there is **no scheduled job and no push or
  email channel in v1**; the flags surface in the renewal calendar and the UI.
- **Seats are a pool with server-side enforcement.** Each subscription has a
  `seats` count; assignment is rejected once active assignments reach it. A seat
  is active while `released_at IS NULL`, so releasing returns capacity to the
  pool. Assign and release both write an audit entry
  (`subscription.seat_assigned` / `subscription.seat_released`) and a
  `seat_change` row in the module's own change history, which also records
  price, plan, cancellation, and renewal events.
- **Cost is per-record; no FX, consistent with Licenses.** Each subscription
  stores `cost_per_cycle`, an ISO-4217 `currency`, and a `billing_cycle`
  (monthly/quarterly/annual). Reports normalize to monthly-equivalent and
  annualized figures and group **by currency — never summing across
  currencies**. Cross-currency roll-up remains the Budget module's job.
- **RBAC and audit-on-view.** `subscriptions.read` (view registry, seats, and
  reports) and `subscriptions.manage` (create/edit/cancel/renew, assign and
  release seats — technician). Viewing lists, detail, spend, or the renewal
  calendar writes **no** audit; only the explicit CSV export
  (`GET /subscriptions/export`) is audited, as `subscription.export`.
- **`payment_ref` is a reference string, not a secret.** It is intended for a
  PO/contract/vendor-account identifier and is stored in plaintext and returned
  in API responses. **Do not put card numbers or credentials in it** — the vault
  is for secrets, and this field deliberately does not use it.
- **`license_ref` is advisory.** For hybrid SaaS-plus-license products a
  subscription may point at a Licenses record. The column carries **no foreign
  key** by design, so the two modules stay independently deployable; a dangling
  reference is tolerated rather than blocking.

### IT budget & expense

- **Control-plane only, and — like Subscriptions — no worker at all.** The
  Budget module (budgets and per-period lines, expense records, FX, renewal
  auto-feeds, variance/forecast reporting) is entirely server-side: **no agent
  capability, no Celery beat sweep, no new compose services, and no new env
  vars.** It reuses Postgres, the audit log, RBAC, the object store, and the
  shared **`vendor`** entity from Licenses. Nothing to configure to run it.
- **This module owns FX for the whole financial trio.** Licenses and
  Subscriptions deliberately store a per-record currency and never convert;
  Budget holds the org's `base_currency` and an operator-maintained `fx_rate`
  table (`from_currency → base`, `effective_date`, `rate`). Rates are entered by
  hand — there is **no external rate feed and no scheduled fetch**, consistent
  with the on-prem-first posture.
- **Conversion is captured at write time, never recomputed on read.** Every
  expense stores both the `fx_rate` it was converted at and the resulting
  `amount_base`, so adding or correcting a rate afterwards changes nothing
  already recorded. Where no rate resolves, the row is still written with
  `amount_base` NULL and is reported as unconvertible — **the module never
  guesses a rate**, and every aggregate discloses what it could not convert
  alongside the total, so a figure is never silently understated.
- **Base-currency changes are not retroactive.** `PUT /budget/settings` is
  audited (`budget.base_currency_changed`) and re-values nothing. Once any
  non-void expense already carries a converted `amount_base` the change is
  rejected unless the caller passes `acknowledge_mixed_basis=true`. There is no
  backfill job and no runbook to re-value history — a report spanning the change
  mixes two bases, and that caveat belongs in front of whoever reads it.
- **The forecast is computed at read time; expected rows are written only on
  request.** Upcoming license renewals and subscription billing cycles are
  projected live from those registries, so the forecast cannot drift. Turning a
  projection into a confirmable `expected` expense happens **only** via
  `POST /budget/expenses/materialize`, which is audited, bounded to a 24-month
  window, and idempotent on `(source, link_id, period_key)` — re-running it
  creates nothing. A **voided** auto-fed expense deliberately stays under that
  key, so a cost an operator retracted is never resurrected by a later run.
- **Expected entries never auto-confirm.** A human confirms each one, and the
  confirmed amount may differ from the projected figure. `POST /budget/expenses`
  defaults to `expected` for exactly this reason — a row is never `confirmed` by
  omission, so nothing books into "actual" without someone asserting it.
- **Invoice attachments** live in the same object store as every other module's
  artifacts, under the per-tenant, per-expense key
  `{org_id}/budget/expenses/{expense_id}/attachment` — matching Backup
  (`{org_id}/backup/...`) and Provisioning (`{org_id}/provision/...`). The key
  is derived entirely server-side and carries **no client-supplied path
  segment, ever** — the client's filename is sanitized and kept only as a
  display name (`invoice_filename`), never as part of the key or reflected
  unsanitized into the download's `Content-Disposition` header. Bytes are
  never returned inline in a list; only the dedicated attachment route serves
  them, and both upload and download are audited.
- **`invoice_ref` is a reference string, not a secret.** It is for an
  invoice/PO number that cross-references the ERP's own record. It is stored in
  plaintext, appears in both CSV exports, and **must not** hold card numbers or
  credentials — the vault is for secrets and this field deliberately does not
  use it (invariant #3).
- **RBAC is stricter here than in any other module, deliberately.** Four
  permissions — `budget.read`, `budget.manage`, `expense.record`,
  `expense.confirm` — with record and confirm split so the person who enters a
  cost need not be the person who asserts it. **Technician is granted none of
  them**; budget and expense data is sensitive even inside IT. Super-admin and
  org-admin get all four; auditor gets `budget.read`.
- **Audit-on-view.** Viewing lists, detail, variance, position, or the forecast
  writes **no** audit event. The only audited reads are the two CSV exports
  (`budget.export`) and an attachment download.
- **The Odoo export is one-way CSV in v1**, emitting a pinned column contract in
  this exact order: `date, reference, vendor, category, cost_center,
  description, amount, currency, amount_base, base_currency, link_type,
  link_id`. Future columns are additive at the end of the row, never a
  reordering — so a downstream import mapping can be built once. The
  general-purpose `GET /budget/expenses/export` is a wider ad-hoc set; **do not
  point an ERP import job at it.** Two-way sync (reading confirmed invoice
  amounts back) is explicitly deferred.
- **`GET /budget/expenses/export/odoo` defaults to CONFIRMED rows only.** The
  pinned column contract above has no `status` column, so an `expected` row —
  a projected/materialized cost nobody has confirmed, potentially at a
  projected (not actual) amount — would otherwise book into Odoo
  indistinguishable from a real, asserted cost. An org that materializes a
  full year of monthly subscriptions and exports at month end would book 11
  renewals that have not happened yet as real spend. Pass
  `?include_unconfirmed=true` as an explicit opt-in if you genuinely want
  `expected` rows included too.

### Service Desk

- **Control-plane only.** The Service Desk module (the ticket core: queues,
  categories, incidents, service requests, comments, attachments, request
  approval, status history) is entirely server-side — **no agent capability,
  no Celery sweep, no new compose services, and no new required env vars**. It
  reuses the existing Postgres, audit log, RBAC, and object store; nothing new
  to provision. This platform now owns ticketing (`docs/00-vision.md` is
  amended deliberately) rather than integrating an external helpdesk — see
  `docs/modules/service-desk/spec.md` §0.1 for why.
- **Two ticket types, one table.** `incident` (something broke, restore
  service) and `request` (fulfil a need) share `ticket`, `ticket_comment`, and
  `ticket_attachment` rather than forking into two schemas — the shared
  "everything I am working on" technician view would otherwise be a `UNION`.
  Each type has its own server-enforced status vocabulary and transition table
  (`lifecycle.py` is the sole authority — never re-derived at a route or in a
  test), including which terminal state is truly final (`cancelled`/
  `rejected`) versus reopenable (`closed`).
- **Service requests carry a per-category approval gate.**
  `ticket_category.requires_approval` decides whether a request enters
  `pending_approval` (creating a `ticket_approval` row) or goes straight to
  `in_progress`. Deciding requires `ticket.approve` and, when the category
  names an `approver_role_id`, membership of that specific role — **a
  requester may never approve their own request**, even holding
  `ticket.approve` outright, enforced server-side regardless of what the UI
  shows.
- **The four-plus-one permission model, with technician's grants called out
  explicitly.** `ticket.read`, `ticket.write`, `ticket.assign`,
  `ticket.approve`, `ticket.admin` (queue/category management). Unlike every
  other financial/governance module in this platform, **technician is granted
  `read`, `write`, and `assign`** — this is a technician's primary daily tool,
  so withholding it the way Budget withholds its own permissions from
  technician would make the module unusable for the role it exists for.
  Technician is denied only `ticket.approve` and `ticket.admin`. Super-admin
  and org-admin hold all five; auditor and read-only hold `ticket.read`.
- **Audit-on-view, with exactly one audited read.** Viewing queues,
  categories, the ticket list, a ticket's detail, comments, or the attachment
  list writes **no** audit event. The one exception is an attachment
  **download**, which is `ticket.read`-gated and audited
  (`ticket.attachment_downloaded`) even though it is a read — the same
  posture Budget's expense-invoice download uses. Every mutation (ticket
  create/update, status change, assign, approve/reject, comment, attachment
  upload, queue/category create/update, and the one-off system-attributed
  `ticket.queues_seeded`/`ticket.categories_seeded` lazy-seed events) is
  audited exactly once, at the layer that performs it.
- **Attachments live in the same per-tenant object store as every other
  module's artifacts**, under the key
  `{org_id}/service-desk/tickets/{ticket_id}/{attachment_id}` — matching
  Budget (`{org_id}/budget/...`), Backup (`{org_id}/backup/...`), and
  Provisioning (`{org_id}/provision/...`). The key is derived entirely
  server-side and carries **no client-supplied path segment, ever**; the
  client's filename is sanitized and kept only as a display name, never
  reflected unsanitized into the download's `Content-Disposition` header.
  Unlike Budget's one-attachment-per-expense fixed slot, a ticket can carry
  many attachments, each its own row/key. Uploads are capped (10 MiB) and
  bounded on both the declared `Content-Length` and the bytes actually read;
  a ticket in a terminal status (`closed`/`cancelled`/`rejected`) accepts no
  new comments and no new attachments.
- **Change management's ticket port now has a real backend here** — see the
  "Change management" section above (`ITS_CHANGE_TICKET_BACKEND=service_desk`).
  This is the first real consumer of Service Desk from another module; the
  link is one-directional (a change may reference the ticket it raised; a
  ticket never drives a change).
- **Notification, the end-user portal, email intake, and the SLA engine are
  separate sub-projects, not present in this build.** This module is the
  ticket core only. There is no outbound webhook/SMTP notification, no
  second (end-user) auth surface, no inbound email receiver, and no SLA
  timer/business-calendar/breach-escalation engine yet — each is specced and
  built on its own, and this module deliberately does not grow a private
  notification path or a private SLA field in the meantime.

### IT documentation: stored documents (files)

The documentation module gained a **file** — a signed vendor contract, a rack
photograph, a site network diagram, a firewall configuration export. Articles,
typed records, links, versions and search are unchanged
(`docs/modules/documentation/spec.md`); this is the sibling spec
`spec-attachments.md`. **Backend only in this build** — the console screen is
its own cycle.

- **Bytes live in the same per-tenant object store as every other module's
  artifacts**, under `{org_id}/docs/documents/{document_id}/{version_id}` —
  matching Service Desk (`{org_id}/service-desk/...`), Budget
  (`{org_id}/budget/...`), Backup and Provisioning. The key is derived
  entirely server-side and carries **no client-supplied path segment, ever**;
  the client's filename is sanitized and kept only as a display name. The
  trailing `{version_id}` is what makes each upload immutable — a new version
  writes a NEW object and never overwrites the previous one, so no upload can
  destroy the only copy of a file.
- **No presigned URL is ever minted for a document, and that is not a
  preference.** `docker-compose.yml` routes `PathPrefix('/${ITS_S3_BUCKET}/')`
  to MinIO on the **same hostname and entrypoint that serves the console**, so
  a presigned GET of an uploaded HTML file would be an unauthenticated,
  same-origin, script-executing URL for the entire tenant. Document bytes
  therefore flow through the API process and nowhere else. Do not add one, and
  in particular **do not run `mc anonymous set download`** on this bucket — the
  warning already in `docker-compose.yml` applies here with teeth.
- **Two limits, and one of them protects your backups.** Per file: **25 MiB**,
  bounded on both the declared `Content-Length` and the bytes actually read.
  Per organisation: **5 GiB** total across all live versions, checked twice per
  upload — once before the body is read, once after. The org quota exists
  because **this bucket is shared with backup repositories**: without it, a
  technician-tier `docs.manage` holder filling the volume stops backups from
  writing. Both are module constants (`app/modules/documentation/documents.py`)
  with no UI in this build; raising the per-file cap raises peak API-process
  memory as `concurrent_uploads x cap`, which is the thing to weigh rather than
  the file size.
- **The stored type is sniffed from the bytes, server-side.** There is no
  multipart parser in this project, so a client cannot declare a content type
  at all. PDF, PNG/JPEG/GIF/WebP, ZIP and OOXML, legacy Office, and UTF-8 text
  of any kind are accepted; UTF-16 and unrecognised binary are refused with a
  message naming the list (zip it, if you need to store it). HTML, SVG and XML
  **are** accepted and are stored as `text/plain` — the sniffer cannot emit
  `text/html` or `image/svg+xml` for any input, so the console shows their
  source as characters and never asks a browser to parse them.
- **Downloads are always download-shaped**: `application/octet-stream`,
  `Content-Disposition: attachment`, `Cache-Control: no-store`, whatever the
  file is. A documentation library holds signed contracts; those should not sit
  in a shared workstation's disk cache.
- **Permissions:** `docs.read` covers reading and downloading (an auditor who
  can see that a contract exists but not read it is not an auditor), and
  `docs.manage` covers creating, uploading and linking — both already exist and
  both keep their current grants. **One new code, `docs.document.purge`**,
  seeded to **super-admin and org-admin only**: it irreversibly destroys a
  stored version's bytes. A technician can archive a document; only an admin
  can destroy one. Nothing else in this module is irreversible.
- **Every upload, every purge and every download is audited** — including
  opening the viewer, because the server cannot tell viewing from saving and an
  audit log should not claim a distinction it cannot verify. Expect audit volume
  on this module to grow with reading, not only with writing.
- **Object storage is a soft dependency here, as everywhere.** With MinIO down
  the documentation page still lists documents from PostgreSQL; only the byte
  routes fail, with a 503 that names the dependency rather than a 500 or an
  empty list.
- **No antivirus scanning, and not pretended.** There is no scanner in this
  stack. What bounds the exposure is structural rather than detective: no agent
  can resolve a docs key, no job type references one, and no presigned URL
  exists — so this platform never hands an uploaded file to an endpoint.

### End-user portal

- **DB role and RLS policies, an API, and a UI — all landed.** Migration
  0028 creates `portal_user` (NOSUPERUSER, NOBYPASSRLS, NOLOGIN — the
  `init-app-user.sh` script below is what gives it a LOGIN password on a
  **fresh** install; see the upgrade note below for an **existing** one)
  and seven table grants scoped to exactly what a requester needs (no
  `ticket_queue` access at all — a requester never picks a queue; migrations
  0029/0030 add a handful more grants the ticket-core write paths need — see
  `docs/modules/portal/spec.md` §2.1). Five of the original seven tables
  additionally get an `AS RESTRICTIVE TO portal_user` `portal_isolation`
  policy, ANDed with the existing PERMISSIVE `tenant_isolation` policy so a
  requester sees only their own rows within their own org — never every
  ticket in the org. See the migration's own comment for why the policy MUST
  be restrictive rather than permissive.
- **New `requester` role**, holding only `portal.ticket.read` /
  `portal.ticket.write` — deliberately not the org-wide `ticket.read`/
  `ticket.write` staff permissions, which would defeat the row-level scoping
  regardless of RLS since they also gate staff routes.
- **`PORTAL_DB_PASSWORD`** (see `.env.example`) provisions the role's LOGIN
  password the same way `APP_DB_PASSWORD` does for `app_user`, and
  **`ITS_DATABASE_PORTAL_URL`** (also `.env.example`, beside
  `ITS_DATABASE_URL`/`ITS_DATABASE_ADMIN_URL`) is what actually wires that
  password to a connection the portal API uses. The portal API
  (`backend/app/modules/portal/api.py`, seven routes under `/api/v1/portal`)
  and the portal UI (`frontend/src/modules/portal/`) both consume the role
  on this branch — this is no longer security-foundation-only.
- **Existing (pre-portal) deployments must be upgraded by hand — this is not
  optional.** `deploy/postgres/init-app-user.sh` is a Postgres entrypoint
  script that runs **only when the data directory is first initialized**. On
  an install that predates the portal, migration 0028 creates `portal_user`
  as `NOSUPERUSER NOBYPASSRLS NOLOGIN` with **no password** — a migration
  cannot know one — and nothing else on an upgrade path ever gives it one, so
  even with `ITS_DATABASE_PORTAL_URL` set correctly the role cannot
  authenticate and every portal request still fails. After running
  `docker compose up migrate` on an existing deployment, connect as
  `its_admin` (or any superuser) and run:
  ```sql
  ALTER ROLE portal_user LOGIN PASSWORD '<the value of PORTAL_DB_PASSWORD in .env>';
  ```
  Use the exact same password already set in `.env`'s `PORTAL_DB_PASSWORD` —
  `ITS_DATABASE_PORTAL_URL` is built from that value, so a mismatch here
  fails authentication with no further clue than "password authentication
  failed for user portal_user". This step is idempotent and safe to re-run
  (it only ever changes the password), and only needs doing once per
  deployment, at the first upgrade to a portal-carrying version.

### Device-group membership is a beat sweep, and beat is inside `worker`

Migration `0072` made device groups a lifecycle (rules, precedence, archive);
membership is recomputed by a Celery beat entry, **`device-group-reevaluate`**,
on the `worker` service's own clock.

- **The cadence is five minutes** (`app/services/device_groups.py::REEVALUATE_INTERVAL_S`,
  read by `worker.py` rather than written as a literal there, so the console
  and the schedule cannot disagree). That interval *is* the lag a membership
  change costs: an edited rule does not move a device until the next sweep.
- **No route enqueues it.** Nothing in this codebase enqueues a Celery task
  from a request — everything is beat-driven — so "save the rule and watch it
  apply" is not a thing this build does, by decision rather than omission.
- **Beat is not a container of its own.** The `worker` service runs
  `celery -A app.worker worker --beat --schedule=/var/lib/its/beat/celerybeat-schedule`,
  so the schedule is read once, at worker start. **A release that adds a beat
  entry does nothing until that container is recreated** — membership rules
  simply never fire, silently, with a healthy-looking stack. `docker compose up
  -d` in the upgrade sequence recreates it because step 3 changes
  `IT_SUITE_VERSION`; if you ever skip step 3, use
  `docker compose up -d --force-recreate worker`. Verify with step 7's
  `create_celery().conf.beat_schedule` read-out.
- **No new compose service, no new `.env` key, no agent capability.**

### Push install (cycle B1): two new permission codes that refuse everything

Migration `0077` lands `push_authorization` and `push_credential_scope`, the
dedicated `push_install` credential type, and **two new permission codes** —
`agent.push.run` and `agent.push.scope.manage` — seeded to **super-admin and
org-admin only**, and deliberately **not** to `technician`, `auditor` or
`read-only`. Migration `0078` adds `devices.provenance`.

- **It dispatches nothing.** A fully authorized request is answered `501`.
  There is no `agent.push_install` job type, no agent capability, and no code
  under `agent/`. That is cycle B2 and the two cycles must not be merged
  (`docs/modules/agent-distribution/spec-push-install.md` §14).
- **Nothing for you to configure.** No new compose service, no new `.env` key,
  no new port, no beat entry. The two console screens appear for accounts
  holding the codes; until an authorization and a credential scope both exist,
  every request is refused and recorded.
- **The safe answer for a domain-joined Windows fleet is still the managed
  path above**, not this one — `spec-push-install.md` §7.4, and it is the
  reason "Publishing the agent to the console" §6 exists.

### The AI admin agent: `.env` is the on switch, and where the model runs is your decision

`docs/modules/ai-admin/spec.md` §0.10, §0.14, §11.2.

**Setting `ITS_AI_*` turns the assistant on for every organization on this
deployment. Not setting it turns it on for none.** That is the whole switch, as
of the operator's decision of 2026-08-25: the model is the same for every
tenant, the API key is `.env`, and the console neither picks a provider nor
offers a per-organization toggle. Nothing is configured by default and nothing
egresses by default — with no `ITS_AI_*` variables set, `load_provider_configs`
returns an empty map, no provider can be resolved, `GET /ai/status` reports the
feature unavailable, and the turn route answers 503. There is no bundled key, no
default base URL and no default model name.

**Exactly one provider may be named.** `ITS_AI_PROVIDERS` holding two keys is a
configuration error raised at startup, naming both, rather than a choice offered
in a dropdown — "the same model for every tenant" and a list of two cannot both
be true. One deployment, one model; a customer who needs a different one gets a
different deployment, which is how this platform is delivered anyway.

Configuration is **server environment only**, matching the
secrets-by-file-path convention `ITS_AUDIT_HMAC_KEY_PATH` already uses:

```
ITS_AI_PROVIDERS=local              # ONE provider key; two is an error
ITS_AI_LOCAL_KIND=openai_compatible # "openai_compatible" | "anthropic"
ITS_AI_LOCAL_BASE_URL=http://vllm.internal:8000/v1
ITS_AI_LOCAL_MODEL=<model name>
ITS_AI_LOCAL_API_KEY_PATH=/var/lib/its/secrets/ai-provider.key   # optional
```

Put them in `deploy/.env` (the `api` service reads it via `env_file`), where
`.env.example` now carries the same block commented out. **Restart the `api`
service after editing them** — the environment is read once, at startup.

**The key path must be on the `its-secrets` volume**, which the `api` service
mounts at `/var/lib/its/secrets` — the same convention `ITS_AUDIT_HMAC_KEY_PATH`
already uses. This is not cosmetic. Nothing in the stack mounts `/run/secrets`,
so a path there is a `FileNotFoundError` at the first hosted-provider call; and
`its-secrets` is the volume `docs/07-backup-restore.md` §2.2 tars, so a key
written anywhere else is key material outside your backup set. Create it the way
you created the audit key:

```sh
docker compose run --rm --no-deps --entrypoint sh api -c \
  'printf %s "<the provider api key>" > /var/lib/its/secrets/ai-provider.key && \
   chmod 600 /var/lib/its/secrets/ai-provider.key'
```

The file is read **at point of use and never cached on the config object**, so a
rotated key takes effect on the next call rather than at the next restart, and a
`repr` of the provider config cannot contain a secret because the object never
holds one.

A missing or misspelled variable for a named provider raises **at configuration
read time, naming the variable**, rather than at 09:00 when an administrator asks
a question — and `create_app` forces that read at startup, so a broken block
fails in your terminal beside the file you just edited. **The base URL is never
org-editable** and there is no field for it anywhere an org-admin can reach; the
provider is not selectable anywhere either. That removes the SSRF primitive
rather than guarding it.

**The on-prem default is `openai_compatible` pointed at a self-hosted endpoint on
your own network** — vLLM, Ollama, llama.cpp, or a customer gateway. Under it,
nothing leaves. The honest cost is quality: small self-hosted models are
materially worse at *tool selection* than frontier models. That is a quality cost
and not a safety one — a wrong tool choice is a wrong or incomplete answer, never
an unsafe action — and nothing in the module is loosened to compensate.

**If you configure an `anthropic` provider, real customer data leaves your
network.** Enumerated so nobody has to guess: the system prompt, the tool
schemas, **the administrator's question text**, and **tool results — hostnames,
ticket subjects and bodies, software inventory, alert text, license and vendor
names, spend figures, audit action names.** What never leaves under either
option: vault material, attachment bytes, session recordings, agent certificates,
and anything belonging to another org.

**Uncommenting that block is the act of consent, and it replaced a tickbox.**
Until 2026-08-25 an org-admin had to accept an audited `egress_acknowledged` on
`PUT /ai/settings` before the feature could be enabled. That gate is gone with
the per-org switch it guarded; the disclosure it stood in front of is not. The
console still shows the list above verbatim, as information rather than as a
checkbox, and `ai.settings_changed` records which provider this deployment was
configured with at the time of each change — so "where was this organization's
data going when this was written" still has an answer in the audit log. The
consent moved from a click by whoever happened to be an org-admin to a line in a
file kept under configuration management by whoever runs the control plane.

**Not enforceable in code, and a deployment requirement rather than a promise:**
no-training and no-retention terms with a hosted provider are **contractual**.
This platform cannot enforce them and does not pretend to. A deployment that must
not egress simply does not configure a provider of that kind — there is no key to
leave in place by accident, because none ships.

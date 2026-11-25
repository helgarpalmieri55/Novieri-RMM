# Deploying Novieri RMM (on-prem, single node)

**Read [`GO-LIVE.md`](GO-LIVE.md) before this faces anything real.** It lists
every default that must be changed — secrets, TLS, the OpenBao seal, the
audit-chain key backup, database backups.

## Status of this runbook

**Rewritten on 2026-11-23 for a new VPS** (`rmm.novieri.com`, design
`docs/superpowers/specs/2026-11-23-vps-y-marca-novieri-design.md`): the
prerequisites, steps 1, 1b, 1c, 6b (new: the gateway certificate's renewal
timer), 7, 8 (the setup code is read from its file now), 10, 11 and 12 of First
boot, [After a reboot or a restart](#after-a-reboot-or-a-restart), and the
upgrade on that VPS. The
certificate on `:443` is Let's Encrypt's, which Traefik asks for and renews
(ACME, TLS-ALPN-01); nothing is copied into `deploy/certs/` any more. The
commands that used a bearer token run inside the stack, or in the console.

⚠ **Rehearsed on 2026-11-24, not yet run on the VPS.** The whole First boot
below was run block by block on 2026-11-24 (plan of 2026-11-23, Task 12), from
`/opt/it-suite/deploy` of a copy of this repository, in a container with Docker
Engine 29.9.3 and Compose 5.7.1; then [After a reboot or a
restart](#after-a-reboot-or-a-restart), with OpenBao restarted and sealed, and
the upgrade's `./scripts/deploy.sh --plan`. Every block ran as written except
for these substitutions:

- **D1, Pebble for Let's Encrypt:** `COMPOSE_FILE=docker-compose.yml:ci/acme-pebble.yml`
  and `PEBBLE_MINICA` in every command's environment, and `--acme-ca-server
  https://pebble:14000/dir` in step 1.
- **D2, the `A` record:** `rmm.novieri.com` in `/etc/hosts`, at the container's
  own address. Step 1c asks the public DNS, so its one refusal,
  `rmm.novieri.com does not resolve to an IPv4 address`, was expected.
- **D2b, the container's HTTPS proxy:** `rmm.novieri.com` added to `no_proxy`.
- **D3:** `--acme-email` given to step 1, which had no terminal to ask on.
- **D4, what you type or paste** reached the same commands on stdin: the three
  shares through `bao write sys/unseal key=-`, the root token to `sh -c` with
  3.4's commands in place of the interactive shell, and the tokens to 3.4's
  `tee` and to the `read` prompts of 3.5 and 11c, each `read` ended by a
  newline as an Enter ends it. What 3.2 and 8 print on screen (the shares, the
  root token, the install code) went to `0600` files instead.
- **D5, the trust root:** Pebble's root for `check-public-tls.sh --cafile` and
  for the `curl`s, and in 11c's agent root store (the system's roots plus
  Pebble's). Step 7's staging-roots block was not run.
- **D6, the console** (steps 8, 9, 11b and 11d): a script sending the requests
  the console's pages send, through Traefik on `:443`. Its 11b token was one
  use, for the Principal site, valid one hour.
- **D7, no systemd:** step 6b's and step 12's services ran their `ExecStart`
  once by hand; `install-backup-timer.sh` ran with `ITS_NO_SYSTEMCTL=1`; no
  `systemctl`, no `journalctl`.
- **D8:** `sudo`, as root.
- **D9:** the copy had no remote, and `deploy.sh --plan` pulls too; for it the
  copy was put on a local branch tracking a local upstream.
- **The base images** `python:3.12-slim`, `node:22-slim` and
  `golang:1.24-bookworm` carried the rehearsal container's HTTPS-proxy CA (in
  `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` and the like, which reach the images
  built from them): `itsuite/backend`, `itsuite/web` and the agent were built
  FROM those. Nothing built here was kept.

**Not rehearsed:** P1, P2 (ufw), P3, P5 and P6 (the account, the firewall, the
updates, installing Docker, the deploy key and the clone are the VPS's); the
real DNS and Let's Encrypt itself; the systemd timers (the backup, the drill,
the gateway certificate); step 7's check of `:8443` from another computer;
the off-host copy (no destination); and the `git pull` of an upgrade from a
real remote. Never run by this repository: a real VPS, the Entra app, an SMTP
relay, and a Windows or macOS endpoint.

What each step printed, against what this runbook shows:

| Step | Printed |
|---|---|
| P4 | nothing to compare: a container has no systemd, so `timedatectl` cannot run |
| 1 | as shown, with Pebble as the CA |
| 1b | **`Recent tags:` said `(none yet)` beside two tags.** Fixed in `build-images.sh` (row 17 of [What was broken](#what-was-broken-and-how-you-would-recognise-it)) |
| 1c | as shown, with D2's refusal; the closing lines it prints are added |
| 2, 3.2, 3.3, 3.5, 3.6 | as shown |
| 3.1 | as shown, but `bao status` exits 2 while sealed; the text is corrected |
| 3.4 | as shown, but OpenBao prints the token fields as `["default" "its-app"]` and `87600h`; corrected |
| 4 | `0248`, the highest revision file; the number shown is updated |
| 4b, 5, 6 | as shown |
| 6b | both units written; the service's command issued a certificate with a new serial |
| 7 | the gateway reports `(healthy)`, and so does the worker once it starts (both corrected), and **`worker` and `backup-replicator` restarted for ever** (row 15). Certificate, `acme.json`, `:8443` and limits as shown |
| 8, 9, 10, 11a, 11b, 11d, 11e, 11f | as shown |
| 11c | as shown; each log line starts with `time=…` (added) |
| 12 | **the first backup failed** (row 16) |
| After a reboot | as shown: `Sealed true`, three shares, `Sealed false`, the table, the certificate, `401`; `503` while the api was stopped |
| Upgrade | `--plan` pulls (the text said it changes nothing; corrected); right after first boot it printed `Already deployed` |

The three defects (rows 15 to 17) are fixed in this repository, and with the
fixes the same stack was upgraded through the runbook's upgrade for this VPS,
in the rehearsal container (`git -C .. pull --ff-only`, `./scripts/deploy.sh
--plan`, `./scripts/deploy.sh`): `worker` up and healthy, `backup-replicator`
up, `Recent tags:` listing three tags, and step 12's backup succeeding, with
the bucket and without it. As on any rehearsal without a public CA,
`deploy.sh` ended `Deployed with warnings` (exit status 1): the API probe
(`API returned 000000`, two codes run together, which deploy.sh now prints as
`API returned 000`), the certificate refusal after `--wait 60`, and, with no
systemd, `no scheduled platform backup`.

Before 2026-11-23, every command of the First boot of the time was executed,
top to bottom, from empty volumes, against Docker Engine 29.3.1 / Compose
v5.1.1, ending with a device enrolled and a signed `hello` job round-tripped
and the audit chain reporting `{"ok":true}`; step 3.5's two token writes,
step 8's password field, step 9's sign-in and steps 11c and 11d, as own
authentication phase 4 (2026-11-15) left them, were the ones no live host had
run since they changed, and step 1's `ITS_BACKUP_OLD_IDENTITY_DB=1` and step
6b of [Upgrading an existing deployment](#upgrading-an-existing-deployment)
are still not run live.

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

**Since own authentication phase 4 (2026-11-15) there is no Keycloak.** The
console and the portal sign in on the platform's own screens -- a password
and a second factor, or Microsoft, and a session cookie -- since phase 1, and
AI apps connect over MCP through the platform's own OAuth server since phase
3 (`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md`). Own
authentication phase 4 removed the `keycloak` and `keycloak-init` services,
their image, their routes and every setting they read. Sections written before
phase 1 still describe token claims where the platform now reads its own
session; the runbook is rewritten with the deployment project (design §9).
What the four phases changed that every deployment needs -- and a host
upgraded from a release before phase 1 crosses all four at once:

- **`ITS_PUBLIC_CONSOLE_URL` is required for any sign-in**, and it must be
  exactly the `https://host[:port]` origin the browser's address bar shows
  (step 1). Unset, or naming another host, port or scheme, every sign-in write
  is refused as `csrf` and nobody can sign in -- first boot completes, then
  the sign-in screen refuses. Passkeys, the reset and invitation links, the
  OAuth server and the recovery command are built from it too. The platform
  sends invitations and password resets itself, through **Settings → Email**.
- **`ITS_AUTH_KEYS_BACKEND=openbao` must be in `.env`** (step 3, the sign-in
  keys). A new `.env` copied from `.env.example` has it; one from before
  phase 1 does not, and without it the api will not start.
- **Phase 1 installs on a fresh database.** Design P7: "no hay datos que
  conservar" -- no account carries over from the old identity provider.
  Migration 0245 is safe on a populated database (it only creates tables),
  but on a host upgraded from before phase 1 an existing technician has no
  password row and **no path to one**: forgot-password needs an existing
  password, an invitation refuses an address that already has an account,
  and the recovery command keeps the password as it is. First boot (step 8)
  mints the one `org-admin`, who sets their password on the setup screen;
  every other technician is invited from **Settings → Users**.
- **A super-admin is never invited** (no organization can grant the role, so
  none can invite or re-invite its holder), and an account without a password
  has no way to get one: forgot-password needs an existing password and a
  second factor, and the recovery command leaves the password as it is. So a
  super-admin is made from a technician who has already signed in -- the
  first-boot `org-admin`, or an invited technician who finished enrolment --
  by the direct database grant in [Publishing the agent to the
  console](#publishing-the-agent-to-the-console) (`super-admin` is not
  grantable from any screen). The account keeps its own password and second
  factor; nothing is mailed.
- **Phase 2's migration 0246** runs with every `up -d` (`migrate`), and
  phase 2 adds **no `.env` key and no OpenBao path**. A host with
  `ITS_AUTH_KEYS_BACKEND=local` in production now refuses to start (it had no
  business running); `grep '^ITS_AUTH_KEYS_BACKEND=openbao' .env` must print
  the line.
- **Sign in with Microsoft is configured in the console**, not in `.env`:
  see [Sign in with Microsoft](#sign-in-with-microsoft-own-authentication-phase-2).
- **Phase 3 moves MCP onto the platform's own OAuth server**
  ([AI connections (MCP)](#ai-connections-mcp)). A `.env` that already holds
  `ITS_MCP_REGISTRATION=open` -- step 2 of the connection steps before phase
  3 -- now opens the platform's own anonymous registration (`POST
  /oauth/register`, advertised as `registration_endpoint`) whenever MCP is
  on. Claude and Claude Code no longer need it, because the platform reads
  their client metadata documents: delete the line unless another app needs
  it (step 7 of "Turning it on"), with `sed -i '/^ITS_MCP_REGISTRATION=/d'
  .env`, and deploy. And every connector made before phase 3 stops working
  (401): each person removes the connector, adds it again and approves on
  the console's consent page, with their second factor confirmed in the last
  ten minutes.
- **The first upgrade past phase 4 starts with `git pull`**, then
  `./scripts/deploy.sh` ([Upgrading an existing
  deployment](#upgrading-an-existing-deployment) says why).
- **An existing `.env` keeps the old identity provider's keys**, which
  `scripts/deploy.sh` names on every run. Nothing in this release reads them,
  but the release before phase 4 does not start without four of them, so
  **keep them** until you close that way back in [Retiring the old identity
  provider's data](#retiring-the-old-identity-providers-data).
  `ITS_DEFAULT_ORG_SLUG` is not one of them: keep it.
- **A host upgraded from before phase 4 still has the old identity
  provider's database and three secret files.** Nothing in this release uses
  them. The way back past phase 4 is the backup taken before that upgrade:
  the newest `~/backups/pre-upgrade-*` whose `alembic_version` is below
  `0248` ([Rolling back a deployment](#rolling-back-a-deployment)). The
  nightly and off-site backups leave the database out from phase 4 on. They
  all stay until you remove them, when you choose: [Retiring the old identity
  provider's data](#retiring-the-old-identity-providers-data).

---

## Prerequisites: a new VPS (Ubuntu Server 24.04 LTS)

This runbook builds one deployment: **`rmm.novieri.com`**, on a VPS that starts
empty (operator decisions B1 and B2, 2026-11-23). The deployment model is:
clone this repository onto the host, run the stack from `deploy/`, and
`git pull` to upgrade. Every command below is meant to be pasted as it is; the
values that only you can supply (the Let's Encrypt contact address, the
OpenBao shares, your passwords) are asked for by a prompt, never written into a
command.

### The machine, and its name

- **Ubuntu Server 24.04 LTS**, 4 vCPU, 8 GB of RAM, 100 GB of SSD, and one
  public IPv4 address **on its network interface**.
- **An `A` record `rmm.novieri.com` → that address, created before the first
  boot.** Traefik asks Let's Encrypt for the certificate the moment it starts,
  and a validation that reaches another machine counts against Let's Encrypt's
  limit of 5 failed authorizations per name and hour.
- **No `AAAA` record for `rmm.novieri.com`.** This deployment is one IPv4
  address: this runbook, `host-network.md` and `scripts/preflight.sh` reason
  about it, and nothing checks that `:443` and `:8443` answer on IPv6. An
  `AAAA` record would send browsers and agents, which try IPv6 first, to an
  address nobody has checked serves this stack — or, if it is another
  machine's, to that machine. Let's Encrypt is no safety net either: it
  retries over IPv4 only on a connection timeout, "not on other types of
  error", so an `AAAA` pointing at a host that answers but is wrong fails
  validation (<https://letsencrypt.org/docs/ipv6-support/>). `scripts/preflight.sh`
  asks the public DNS, not `/etc/hosts`, for the A record (the AAAA check
also reads `/etc/hosts`), and refuses either mistake.
- **Inbound 22 (SSH), 443 (console, API, object storage) and 8443 (agents),
  and nothing else.** Both 443 and 8443 must be reachable **from every managed
  endpoint**, not only from operators: `8443` is the agent gateway, and `443`
  carries object storage under presigned grants, which is how backup chunks and
  `app.deploy` artifacts travel. `:80` stays shut: the certificate's challenge
  (TLS-ALPN-01) is answered on `:443`.
- **On a shared or multi-homed host, read `host-network.md` first** (the bind
  address, the reply path, and the fact that ufw does not filter
  Docker-published ports). A dedicated VPS needs none of it.

### P1 — An operator account (as root, once)

If your provider gave you a sudo account instead of `root`, use that account as
the operator and skip this step. Otherwise, logged in as `root`:

```sh
adduser --gecos "" rmmadmin
usermod -aG sudo rmmadmin
install -d -m 700 -o rmmadmin -g rmmadmin /home/rmmadmin/.ssh
install -m 600 -o rmmadmin -g rmmadmin /root/.ssh/authorized_keys /home/rmmadmin/.ssh/authorized_keys
```

`adduser` asks for the new account's password twice; `sudo` will ask for it.
Then log out, log in again as `rmmadmin` with the same key, and check:

```sh
id -nG
```

The list contains `sudo`. Every command from here on runs as this account.

### P2 — The firewall

```sh
sudo ufw allow 22/tcp
sudo ufw allow 443/tcp
sudo ufw allow 8443/tcp
sudo ufw --force enable
sudo ufw status verbose
```

Check that the output says `Status: active`, `Default: deny (incoming)`, and
`ALLOW IN` for `22/tcp`, `443/tcp` and `8443/tcp`. ⚠ **UNVERIFIED** in this
repository's rehearsal (Task 12 of the plan of 2026-11-23 ran in a container,
without ufw). Docker's own documentation warns that ports Docker publishes
bypass ufw ("when you expose container ports using Docker, these ports bypass
your firewall rules"): this stack publishes 443 and 8443 and nothing else, so
the firewall's job here is everything that is not Docker.

### P3 — Security updates

```sh
sudo apt-get update
sudo apt-get install -y unattended-upgrades
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' | sudo tee /etc/apt/apt.conf.d/20auto-upgrades
systemctl is-active unattended-upgrades
```

The `tee` prints the two lines it wrote, and the last command prints `active`.
Ubuntu installs `unattended-upgrades` by default and these are its default two
lines; writing them makes the state explicit rather than assumed.

### P4 — The clock

```sh
timedatectl status | grep -E 'System clock synchronized|NTP service'
```

```
System clock synchronized: yes
              NTP service: active
```

If either says otherwise: `sudo timedatectl set-ntp true`, and check again.
Certificates, second-factor codes and the audit log's times all depend on it.

### P5 — Docker CE, from Docker's repository

**Do not use Ubuntu's `docker.io` package.** An Engine too old refuses
`traefik:v3.6`'s Docker provider, and Traefik then discovers no containers and
every route answers a bare 404 while everything looks healthy. These are
Docker's own Ubuntu instructions (docs.docker.com/engine/install/ubuntu),
starting with its first: remove the distribution's packages that conflict
with Docker's (some VPS images ship one; none installed is fine):

```sh
sudo apt-get remove -y $(dpkg --get-selections docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc 2>/dev/null | cut -f1)
sudo apt-get install -y ca-certificates curl git openssl iproute2
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and in again, so the new group applies, then check the two versions
that matter:

```sh
docker version --format '{{.Server.Version}}'
docker compose version --short
```

The Engine must be **26 or newer** and Compose **2.24 or newer**
(`scripts/preflight.sh` refuses older ones). Compose 2.24 is needed because
`.env` is both Compose's project env file and the services' `env_file`, and
entries like
`ITS_DATABASE_URL=postgresql+asyncpg://app_user:${APP_DB_PASSWORD}@postgres:5432/itsuite`
rely on Compose interpolating `${APP_DB_PASSWORD}` inside the env file.

The `docker` group "grants root-level privileges to the user" (Docker's
post-installation notes): that is why only the operator account is in it.

### P6 — The checkout

Over SSH with a **deploy key** — a read-only key scoped to this repository:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/novieri_deploy -C "novieri-deploy@$(hostname)"
cat ~/.ssh/novieri_deploy.pub
```

Paste that whole line (`ssh-ed25519 AAAA…`, one line) into the repository's
**Settings → Deploy keys → Add deploy key**, with "Allow write access" left
**unchecked**. A terminal soft-wraps that line, and copying the wrapped display
inserts newlines GitHub rejects as "not in OpenSSH public key format";
`ssh-keygen -l -f ~/.ssh/novieri_deploy.pub` prints a fingerprint if the file
itself is sound. Then:

```sh
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/novieri_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
printf '%s\n' 'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl' >> ~/.ssh/known_hosts
ssh -T git@github.com
```

The `printf` line is GitHub's published Ed25519 host key
(docs.github.com, "GitHub's SSH key fingerprints": `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`),
so the first connection asks nothing: trust on first use is not how this host
meets the repository it will run. "Hi …! You've successfully authenticated, but
GitHub does not provide shell access." is success. Then:

```sh
sudo mkdir -p /opt/it-suite && sudo chown "$USER" /opt/it-suite
git clone git@github.com:helgarpalmieri55/Novieri-RMM.git /opt/it-suite
cd /opt/it-suite/deploy
git -C .. rev-parse --abbrev-ref HEAD
```

The last line prints `main`: a deployment is always made from `main`. All
commands below are run from `/opt/it-suite/deploy`.

---

## First boot

Start from genuinely empty state. If you are re-running after a failed attempt,
`docker compose down -v` first — a half-initialised Postgres data directory or
OpenBao barrier will hide the step that actually broke — and `rm .env`, because
step 1 refuses to overwrite one. **A first boot after `down -v` asks Let's
Encrypt for a new certificate**: more than 5 in a week for `rmm.novieri.com`
and production refuses, so a rehearsal uses staging (step 1).

### Step 1 — `.env`

```sh
./scripts/new-env.sh --host rmm.novieri.com
```

It asks for one thing: the contact address Traefik gives Let's Encrypt when it
registers its account. Let's Encrypt stopped mailing expiry warnings on
2025-06-04 and no longer keeps the address on the account (it passes it to
ISRG's mailing list), so nothing about renewal depends on it. Then it
writes `.env` from `.env.example`, mode 600: a fresh random value in every
`change-me` except the two OpenBao tokens, `rmm.novieri.com` in every URL, and
the ACME keys. It prints no secret, and it refuses to overwrite an existing
`.env`.

```
wrote .env (mode 600)
  host            rmm.novieri.com
  ITS_ACME_EMAIL=…
  ITS_ACME_CA_SERVER=https://acme-v02.api.letsencrypt.org/directory
  ITS_S3_PUBLIC_ENDPOINT=https://rmm.novieri.com
  ITS_GATEWAY_URL=wss://rmm.novieri.com:8443/agent
  ITS_GATEWAY_RELAY_URL=wss://rmm.novieri.com:8443/relay
  ITS_PUBLIC_CONSOLE_URL=https://rmm.novieri.com
  new secrets     POSTGRES_ADMIN_PASSWORD APP_DB_PASSWORD PORTAL_DB_PASSWORD MINIO_ROOT_PASSWORD
  still change-me ITS_OPENBAO_TOKEN OPENBAO_TOKEN (README step 3.5 fills them)
```

```sh
stat -c %a .env
grep -c '=change-me$' .env
```

```
600
2
```

Exactly two `change-me` — the OpenBao pair, which step 3.5 fills. Real secrets
never enter git; `.env` is gitignored.

**A rehearsal** — a second first boot, a test of this runbook on another
machine — uses Let's Encrypt's staging directory instead, whose certificates no
browser trusts and whose limits are far higher:
`./scripts/new-env.sh --host rmm.novieri.com --acme-ca-server https://acme-staging-v02.api.letsencrypt.org/directory`.
On a staging rehearsal, step 7's check needs the staging roots, and a later
`./scripts/deploy.sh` prints **two** warnings and ends `Deployed with
warnings` (exit status 1): `API returned 000 (expected 401)`, because its
curl probe of `:443` has no staging CA, and the certificate refusal after its
`--wait 60`, which has none either. Everything is up; those lines are the
probes, not a failure.

`ITS_PUBLIC_CONSOLE_URL` is the origin every sign-in write is checked against;
it must stay exactly what the browser's address bar shows, `https://rmm.novieri.com`.

**The per-address sign-in limits are optional** (`ITS_AUTH_LOGIN_IP_LIMIT` and
`ITS_AUTH_FACTOR_IP_LIMIT`). Unset, one address may send 30 requests in 10
minutes to the password step, and 30 to the second-factor steps; technicians
behind one office NAT address share one budget. Raise them for that, within
5-1000 (`.env.example` gives the costs); step 7 shows the values the api
started with.

### Step 1b — Tagged images

```sh
./scripts/build-images.sh
v="$(git -C .. rev-parse --short HEAD)" && sed -i '/^IT_SUITE_VERSION=/d' .env && echo "IT_SUITE_VERSION=$v" >> .env && grep '^IT_SUITE_VERSION=' .env
```

`build-images.sh` builds `itsuite/backend` and `itsuite/web` tagged with the
checkout's commit, and ends with `Recent tags:` — on this new host, the one tag
it just built. **That list is the rollback target list, and there is no earlier
entry:** until step 12 has taken the first backup, a new host has no way back
but starting over (see [Rolling back a deployment](#rolling-back-a-deployment)).
The second line writes the tag into `.env`, deleting any earlier line first.
(Until 2026-11-24 this list read `(none yet)` whatever tags existed: row 17 of
[What was broken](#what-was-broken-and-how-you-would-recognise-it). A
`(none yet)` printed before then said nothing about a host's tags.)

### Step 1c — The pre-flight

```sh
./scripts/preflight.sh
```

It checks `.env` (mode, secrets, tag), that `ITS_PUBLIC_HOST` is the console's
host, that `rmm.novieri.com` resolves to this machine and has no `AAAA`
record, that 443 and 8443 are free, and the Docker versions. A good first boot
ends:

```
  note    ITS_OPENBAO_TOKEN is still change-me -- expected before first boot; §3.2/§3.4 issue it
  note    OPENBAO_TOKEN is still change-me -- expected before first boot; §3.2/§3.4 issue it
  ...
  ok      ITS_PUBLIC_HOST=rmm.novieri.com is the console's host
  ok      ITS_ACME_EMAIL is set
  ok      ACME CA: Let's Encrypt production
  ok      rmm.novieri.com resolves to …, an address of this host
  ok      443 and 8443 are free
  ok      Docker Engine …
  ok      Docker Compose …
  ok      .env is gitignored

No known-bad state found. This does not mean the deployment is correct --
work through GO-LIVE.md §2 (the first administrator), §4 (OpenBao seal), §5 (gateway cert)
and §6 (back these up) before anything real touches it.
```

Any `REFUSE` names its fix; correct it and run the pre-flight again before
going on.

### Step 2 — Postgres

```sh
docker compose up -d postgres
```

First boot runs `postgres/init-app-user.sh`, which creates `app_user` and
`portal_user` (both NOSUPERUSER, NOBYPASSRLS), grants `its_admin` BYPASSRLS
(the migration/bootstrap role), and installs `app_user`'s standing `ALTER
DEFAULT PRIVILEGES` so any table a future migration adds is usable with no
manual step. No table exists yet, so `app_user` cannot use anything yet
either — **step 4b is not optional**.

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

`Initialized false` (and, below these lines, `Sealed true`) is correct. Its
exit status is 2, not 0: `bao status` exits 2 for any sealed vault, initialised
or not. (The `bao` CLI defaults to
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
| `its-app` | `/bao/its-app.hcl` | api, gateway, worker (`ITS_OPENBAO_TOKEN`) | sign jobs, seal/unseal org secrets, sign agent CSRs, HMAC passwords with the sign-in pepper, sign the platform's own tokens |
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

then, inside that shell, this line on its own -- it waits for you to paste
the root token and press Enter, and a `read` given more pasted lines would
take the next one as the token:

```sh
stty -echo; read -r BAO_TOKEN; stty echo; echo; export BAO_TOKEN   # paste the root token, Enter
```

and then:

```sh
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
`token_policies ["default" "its-app"]`, the second with
`token_policies ["default" "gateway-issuer"]` — both `token_duration 87600h`.

The `auth tune` is required: without it the default 32-day system max TTL caps
the tokens, and the entire platform stops signing jobs a month after go-live.
The 10-year TTL is a deliberate trade — nothing in the stack renews these tokens
— so put a rotation reminder in GO-LIVE.md's schedule rather than trusting it.

`-orphan` matters: a child of the root token dies when the root token is
revoked.

Keep the `gateway-issuer` token where step 6 and the step 6b timer can
read it, and nowhere else:

```sh
sudo install -d -m 700 /etc/it-suite
sudo install -m 600 /dev/null /etc/it-suite/gateway-issuer.token
sudo tee /etc/it-suite/gateway-issuer.token >/dev/null   # paste, Ctrl-D
```

#### 3.5 Put both tokens in `.env`

Run these three one block at a time: each of the first two asks for one
token, without echoing it, and waits for your paste -- the `its-app` token
from 3.4, then the root token from 3.2. `read` and `printf` are shell
builtins, so neither token becomes an argument of any process, and each
deletes the key's earlier value first, because a second line further down
silently wins:

```sh
read -rsp 'its-app token (3.4): ' T && echo && [ -n "$T" ] && sed -i '/^ITS_OPENBAO_TOKEN=/d' .env && printf 'ITS_OPENBAO_TOKEN=%s\n' "$T" >> .env; unset T
```

```sh
read -rsp 'root token (3.2): ' T && echo && [ -n "$T" ] && sed -i '/^OPENBAO_TOKEN=/d' .env && printf 'OPENBAO_TOKEN=%s\n' "$T" >> .env; unset T
```

```sh
grep -c '^ITS_OPENBAO_TOKEN=\|^OPENBAO_TOKEN=' .env
```

```
2
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

**Run in CI since own authentication phase 4.** The `deploy` job initialises
OpenBao (one share), loads `its-app`, runs `openbao-init` and starts the api
against it, then signs a person in and connects an AI app end to end through
Traefik (`deploy/ci/mcp_flow.py`): transit `hmac` with the `auth-pepper` key
and `sign` with `auth-signing` run live there. Five shares, the unseal by
hand and a host reboot have still only been run by the people who wrote
this section.

Since own authentication phase 1 it also creates two transit keys for sign-in:
`auth-pepper` (HMAC, the versioned pepper every password verifier is
computed with) and `auth-signing` (ECDSA P-256, which signs the assistant's
internal tokens and, from phase 3, MCP access tokens). Neither key ever leaves
OpenBao. **On an install that ran `openbao-init` before, re-run it and re-load
the policy** (`bao policy write its-app /bao/its-app.hcl`, as in 3.4), or the
API cannot verify a password. **And add `ITS_AUTH_KEYS_BACKEND=openbao` to an
existing `.env`** (a new one copied from `.env.example` has it): without it the
api uses the `local` backend, which with `ITS_ENVIRONMENT=production` is
refused at start (`LocalAuthKeysInProduction`), whether or not its files exist,
so the api restart-loops:

```sh
sed -i '/^ITS_AUTH_KEYS_BACKEND=/d' .env && echo 'ITS_AUTH_KEYS_BACKEND=openbao' >> .env
grep '^ITS_AUTH_KEYS_BACKEND=' .env
```

```
ITS_AUTH_KEYS_BACKEND=openbao
```

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
docker compose exec -T postgres \
  psql -U its_admin -d itsuite -tAc 'select version_num from alembic_version;'
```

```
0248
```

That must equal the highest file in `backend/migrations/versions/`, and it is
the one number in this runbook that is stale the moment it is written. (The
first-boot run reached `0048`; the chain was at `0079` on 2026-08-26, and at
`0248` when this runbook was rehearsed on 2026-11-24 —
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
  --entrypoint python gateway /tmp/issue-gateway-cert.py rmm.novieri.com
```

```
OpenBao gateway-issuer token:
gateway.crt  /var/lib/its/tls/gateway.crt  (CN=rmm.novieri.com, 365d)
gateway.key  /var/lib/its/tls/gateway.key  (mode 0600)
root.crt     /var/lib/its/ca/root.crt  (agent client CA)
serial       57:d6:c9:f1:0a:85:72:29:6d:b0:3f:66:8a:1e:40:89:28:ee:a4:46
```

Or non-interactively, from the 0600 file step 3.4 wrote — note `-T`, which is
what leaves stdin available:

```sh
sudo cat /etc/it-suite/gateway-issuer.token | docker compose run --rm --no-deps -T \
  -v "$PWD/scripts/issue-gateway-cert.py:/tmp/issue-gateway-cert.py:ro" \
  --entrypoint python gateway /tmp/issue-gateway-cert.py rmm.novieri.com
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
Step 6b's renewal timer reads it as root, which is the reason for the
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
honoured when it is *already* in the environment (that is what the step 6b
timer's `EnvironmentFile=` is for); nothing here constructs it.

It runs through the `gateway` service because that is the only container
mounting both `its-gateway-tls` and `its-ca`, so the private key is written
straight to the volume the gateway reads and never touches your shell history
or the host filesystem. `root.crt` is the CA the gateway admits agent client
certificates against (`ITS_GATEWAY_CLIENT_CA`).

**This certificate is 365 days and does not renew itself.** Step 6b, next,
installs the timer that renews it every month.

### Step 6b — Renew the gateway's certificate every month

The certificate step 6 issued is valid for 365 days and **does not renew
itself**, and it is the only server certificate every agent dials: the day it
expires, the whole fleet disconnects at once. This timer re-issues it on the
first of every month, as root, from the `0600` token of step 3.4 (the section
"Renewing the gateway certificate" below says why a redirect is right here and
a pipe in step 6):

```sh
sudo tee /etc/systemd/system/its-gateway-cert.service >/dev/null <<'EOF'
[Unit]
Description=Renew the it-suite gateway server certificate
[Service]
Type=oneshot
WorkingDirectory=/opt/it-suite/deploy
# The token arrives on STDIN from a 0600 file, never as an argument. `-T` is
# what keeps stdin available to redirect.
ExecStart=/bin/bash -lc 'docker compose run --rm --no-deps -T \
  -v "$PWD/scripts/issue-gateway-cert.py:/tmp/issue-gateway-cert.py:ro" \
  --entrypoint python gateway /tmp/issue-gateway-cert.py rmm.novieri.com \
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
sudo systemctl list-timers its-gateway-cert.timer --no-pager
```

The last line lists `its-gateway-cert.timer` with `its-gateway-cert.service`
under ACTIVATES and the first of next month under NEXT. Connected agents
reconnect on their own within seconds of each renewal ([Renewing the gateway
certificate](#renewing-the-gateway-certificate)). ⚠ **UNVERIFIED**: the
rehearsal (plan of 2026-11-23, Task 12) ran in a container without systemd;
it wrote both units and ran the service's command once by hand.

### Step 7 — Start everything; Traefik obtains the certificate for `:443`

```sh
docker compose up -d
docker compose ps -a --format 'table {{.Service}}\t{{.Status}}'
```

```
SERVICE             STATUS
api                 Up … (healthy)
backup-replicator   Up …
gateway             Up … (healthy)
migrate             Exited (0) …
minio               Up … (healthy)
openbao             Up … (healthy)
openbao-init        Exited (0) …
postgres            Up … (healthy)
postgres-grants     Exited (0) …
redis               Up …
traefik             Up …
web                 Up … (healthy)
worker              Up … (healthy)
```

The three `Exited (0)` services are the one-shot jobs; that is success.

**The certificate.** Traefik asks Let's Encrypt for `rmm.novieri.com` as soon as
it sees the routers (TLS-ALPN-01, answered on `:443`), stores it in the
`traefik-acme` volume, and renews it by itself 30 days before it expires. Until
it has one — and for good if it cannot get one — it serves its own self-signed
"TRAEFIK DEFAULT CERT" **without failing**. So check, rather than trust:

```sh
./scripts/check-public-tls.sh
```

```
ok      127.0.0.1:443 presents a trusted certificate for rmm.novieri.com
        issuer=C = US, O = Let's Encrypt, CN = …
        X509v3 Subject Alternative Name:
            DNS:rmm.novieri.com
        notAfter=…
```

It waits up to three minutes for the first issuance, and it also refuses a
trusted certificate with **fewer than 28 days left**: Traefik renews at 30, so
that is a renewal that has been failing (Let's Encrypt no longer mails expiry
warnings). A `REFUSE` says what a browser would refuse; `docker compose logs traefik | grep -i acme` says why
Let's Encrypt did not issue (the usual causes: the `A` record is not this
machine's yet, `:443` closed at the provider's firewall, or an `AAAA` record
of another machine). Fix the cause and `docker compose restart traefik`.

On a **rehearsal against Let's Encrypt staging** the check refuses by design:
staging's roots are in no trust store. `deploy.sh` has no such CA either, so a
rehearsal's `deploy.sh` prints two warnings, the API probe (`API returned
000 (expected 401)`) and the certificate refusal after `--wait 60`, and
ends `Deployed with warnings` with exit status 1 although everything is up.
Give the check the roots (the four Let's Encrypt lists on its staging page):

```sh
curl -fsS https://letsencrypt.org/certs/staging/letsencrypt-stg-root-x1.pem https://letsencrypt.org/certs/staging/letsencrypt-stg-root-x2.pem https://letsencrypt.org/certs/staging/gen-y/root-ye.pem https://letsencrypt.org/certs/staging/gen-y/root-yr.pem > /tmp/le-staging-roots.pem
./scripts/check-public-tls.sh --cafile /tmp/le-staging-roots.pem
```

The certificate store Traefik writes must be readable by Traefik alone —
Traefik refuses a looser `acme.json` itself, and the named volume is what
creates its directory:

```sh
docker compose exec traefik stat -c %a /etc/traefik/acme/acme.json
```

```
600
```

⚠ **UNVERIFIED with Let's
Encrypt itself**: the rehearsal (plan of 2026-11-23, Task 12) and every CI run
obtain it from Pebble, Let's Encrypt's own test CA, by the same path; the
issuer line above is the one production's will be.

**The agent-facing side must be the internal CA**, not Let's Encrypt:

```sh
echo | openssl s_client -connect 127.0.0.1:8443 -servername rmm.novieri.com 2>/dev/null \
  | openssl x509 -noout -issuer -ext subjectAltName
```

```
issuer=CN = Novieri RMM internal CA
X509v3 Subject Alternative Name:
    DNS:rmm.novieri.com
```

Two different issuers on the two ports is the correct picture. If `:8443`
shows a Let's Encrypt issuer, an agent will never connect. The likeliest
cause is an absent or unhealthy gateway: with no TCP router, Traefik answers
`:8443` itself with the public certificate, which agents refuse because they
trust only the CA pinned at enrollment. Check `docker compose ps gateway`
first.

**From a computer outside this VPS**, once: the check above dials this
machine from itself and never crosses your provider's firewall, and Let's
Encrypt's validation proves `:443` from the Internet but not `:8443`. On a
Linux or macOS computer elsewhere, `openssl s_client -connect rmm.novieri.com:8443 -servername rmm.novieri.com </dev/null 2>/dev/null | openssl x509 -noout -issuer`
prints `issuer=CN = Novieri RMM internal CA`; on Windows,
`Test-NetConnection rmm.novieri.com -Port 8443` prints `TcpTestSucceeded : True`.
Anything else: open `8443/tcp` at the provider's firewall.

**The per-address sign-in limits the api started with** (step 1):

```sh
docker compose exec api python -c "from app.core.config import get_settings; s = get_settings(); print(s.auth_login_ip_limit, s.auth_factor_ip_limit)"
```

```
30 30
```

### Step 8 — Claim the installation

**This replaces what used to be three steps: creating the first administrator by
piping Python into `docker compose exec`, inserting the organization row by SQL,
and granting the role by a second SQL statement.** All three now happen in one
transaction behind one screen, through the same service code the Users tab uses.

The setup screen sets the administrator's password itself and sends **no
mail**; the first sign-in enrols the second factor (step 9). Account mail
(invitations, password resets) is configured afterwards in Novieri RMM at
**Settings → Email**.

Then:

1. **Open `https://rmm.novieri.com/`.** On an installation with no users it
   shows the setup screen rather than redirecting to sign-in. That first request
   is also what **generates** the install code — nothing runs earlier, so do this
   before going to look for it.

2. **Read the code.** The api writes it to `/var/lib/its/secrets/setup-code`,
   mode 600, when the first request asks for it (and logs it once):

   ```sh
   docker compose exec api cat /var/lib/its/secrets/setup-code
   ```

3. **Enter it**, with your name, email, organization name and the password you
   will sign in with (12 characters at least).

That creates the organization, the administrator account and its `org-admin`
grant **in one transaction** — either all three or none — and writes every part
of it to the hash-chained audit log.

**The organization's slug is `ITS_DEFAULT_ORG_SLUG` from `.env`** — `novieri`
in the shipped example — not something you type, and not derived from the name
you enter unless that setting is blank. That is deliberate: the sign-in, the
OAuth server for AI apps and the recovery command all look people up in that
one organization, and an organization created under any other slug is one its
own administrator cannot sign in to.

**The install code is the only thing between a stranger and this box.** An
appliance answers on the network long before its owner sits down at it, and this
screen must answer before any user exists, so it cannot be gated by a permission.
Zero users alone is not a guard. Claim it promptly, from a browser you trust, and
treat the code as a password until you have.

### Step 9 — Sign in, and enrol your second factor

Setup does not sign you in. Open `https://rmm.novieri.com/sign-in` and sign
in with the address and the password you gave the setup screen. The first
sign-in asks for a second factor before anything else -- an authenticator
app (TOTP) or a passkey -- and then shows ten recovery codes, once. Keep them
where you keep the OpenBao shares: an administrator who loses every factor
needs one, or the [recovery
link](#recovery-link-an-administrator-who-lost-every-second-factor).

There is no identity admin console behind this product any more. Everything
an administrator does with accounts -- inviting, disabling, resetting second
factors, signing someone out everywhere -- is in **Settings → Users**, and
Microsoft sign-in is in **Settings → Microsoft sign-in**.

### Step 10 — Confirm you really are an administrator

**Do not skip this.** Under the old runbook the role grant was a separate SQL
statement and the classic failure was an account that authenticated and could do
nothing. The claim now grants `org-admin` in the same transaction, so the check
is quick — but "the code grants it" and "this deployment granted it" are
different claims, and only one of them is about your box.

In the console: **Settings → Users** lists your account with the **org-admin**
role. Or from inside the stack, with no token at all (the console's API reads a
session cookie, not a bearer token, since own authentication phase 1, so a
`curl` with `Authorization: Bearer` is not a way to ask it):

```sh
docker compose exec -T postgres psql -U its_admin -d itsuite -tAc "select u.email, r.name from users u join user_roles ur on ur.user_id = u.id join roles r on r.id = ur.role_id where u.kind = 'staff'"
```

One line: the address you gave the setup screen, then `|org-admin`.

No line, or a role other than `org-admin`, means the role is not there. Seeded
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
  printf "  %-28s " "$p"; curl -s -o /dev/null -w "%{http_code}\n" https://rmm.novieri.com$p
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

In the console, at `https://rmm.novieri.com/enrollment`: a new token named
`first endpoint`, valid for 24 hours, **one** use, for the site **Principal**
of your own organization's client. The console shows the token **once**; keep
it for the next step and nowhere else. (Since own authentication phase 1 the
console's API takes a session cookie, so the `curl -H "Authorization: Bearer …"`
this step used to show cannot mint one; the console is the way, audited as
`enrollment.token_created`.)

#### 11c — A throwaway agent on this VPS

The first endpoint is this server itself, for a few minutes: the agent built
from this checkout (Go in a container, as your user: the VPS needs no
toolchain, and the binary in `agent/dist/` is yours, not root's), run in a
container that shares the host's network and trusts the host's certificate
roots. It reaches `https://rmm.novieri.com` through the same name, ports and
TLS as an endpoint — the internal CA, mutual TLS on `:8443` — but its packets
never leave this machine, so it does not cross your provider's firewall: step
7's check from outside is what proves that.

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/../agent:/src" -w /src -e CGO_ENABLED=0 -e GOCACHE=/tmp/go-cache -e GOMODCACHE=/tmp/go-mod golang:1.24-bookworm go build -trimpath -o dist/novieri-agent ./cmd/agent
install -d -m 700 /tmp/smoke-agent
```

The token from 11b, at a prompt that does not echo it. This block is one
command on purpose: a prompt ends its block, so nothing pasted after it can be
read as the token.

```sh
read -rsp 'Enrollment token (11b): ' T && echo && [ -n "$T" ] && (umask 077 && printf '%s' "$T" > /tmp/smoke-agent/enroll-token); unset T
```

Then the agent itself:

```sh
docker run -d --name smoke-agent --network host --user "$(id -u):$(id -g)" \
  -v "$PWD/../agent/dist/novieri-agent:/usr/local/bin/novieri-agent:ro" \
  -v /etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro \
  -v /tmp/smoke-agent:/var/lib/novieri-agent \
  -e ITS_AGENT_ENROLL_URL=https://rmm.novieri.com -e ITS_AGENT_DATA_DIR=/var/lib/novieri-agent \
  busybox:1.36.1 /usr/local/bin/novieri-agent
for _ in $(seq 1 30); do docker logs smoke-agent 2>&1 | grep -q 'gateway connected' && break; sleep 2; done
docker logs smoke-agent 2>&1 | grep -E 'starting|enrolling|enrolled|gateway connected'
```

```
time=… level=INFO msg="novieri-agent starting" version=dev data_dir=/var/lib/novieri-agent enrolled=false
time=… level=INFO msg=enrolling url=https://rmm.novieri.com/enroll/v1 hostname=… os_family=linux via_install_link=false
time=… level=INFO msg=enrolled device_id=… gateway_url=wss://rmm.novieri.com:8443/agent cert_not_after=…
time=… level=INFO msg="gateway connected" url=wss://rmm.novieri.com:8443/agent
```

`enrolled` then `gateway connected` is the whole handshake: the CSR signed by
the internal CA, the certificate and the CA pinned in the data directory, the
outbound mutual-TLS WebSocket up. The token file is gone (`ls /tmp/smoke-agent`
no longer lists `enroll-token`): the agent deletes it the moment it has a
certificate. The device appears in the console's **Devices**, online.

The packaged installers for real endpoints — the `.deb`, the macOS script, the
Windows installer and the Group Policy MSI — are in
[Publishing the agent to the console](#publishing-the-agent-to-the-console).
⚠ **UNVERIFIED**: no Windows or macOS endpoint has been enrolled by anybody on
this project; `docs/modules/agent-distribution/first-run-verification.md` is
the procedure for that.

#### 11d — Round-trip a signed job

In the console, on the device from 11c: **Run job → Collect inventory**. Its
row in **Job history** reaches **Succeeded** within a minute. From inside the
stack, the same fact for the job a person asked for (the platform also
schedules an inventory of its own when a device first connects, so the query
leaves that one out):

```sh
docker compose exec -T postgres psql -U its_admin -d itsuite -tAc "select type, status, signature is not null from jobs where requested_by is not null order by issued_at desc limit 1"
```

```
inventory.collect|succeeded|t
```

That is the platform working: the server signed the job envelope with the
Ed25519 key that never leaves OpenBao, the agent verified the signature before
executing anything (there is no unsigned command path), and the result came
back up the same mTLS connection.

#### 11e — The audit chain

From inside the stack, with no token (the same check `docs/07-backup-restore.md`
runs after a restore):

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
novieri {"ok": true, "count": …, "broken_at": null}
```

The console shows the same check for the signed-in person's organization:
**Audit log → Verify chain**. `ok: true` over a chain that includes the
enrollment, the dispatch and the result is the end-to-end proof.

#### 11f — Retire the throwaway agent

```sh
docker rm -f smoke-agent
rm -rf /tmp/smoke-agent
```

The second line removes the device's private key and certificate. The device
stays in the console as a record, offline; remove it there if you do not want
it listed.

The MinIO bucket (`ITS_S3_BUCKET`) needs no provisioning step — `ObjectStore`
creates it on first use.

### Step 12 — The first backup, so that there is a way back

```sh
sudo ./scripts/install-backup-timer.sh
sudo systemctl start its-platform-backup.service
journalctl -u its-platform-backup.service --no-pager -n 20
sudo cat /var/lib/it-suite/backup/last-success
```

The last line prints the time of the backup just taken. Its objects line says
how many files the bucket held; if nothing has stored an object yet (the
backup replicator creates the bucket within a minute of step 7), it says `the
bucket it-suite does not exist yet (nothing has been stored): nothing to
mirror`, and the backup still succeeds. From now on one runs
every night at 02:30 ([Platform
backups](#platform-backups-nightly-off-host-and-a-monthly-drill)). **Until this
step, a new host has no way back at all**: `build-images.sh` listed one tag
(step 1b), the one running. After it, the way back is restoring this backup
([Rolling back a deployment](#rolling-back-a-deployment)).

**That backup is on this VPS, and goes with it.** Losing the machine or its
disk loses every backup with it, and on a new host restoring a backup is the
only way back. Sending each night's backup somewhere else, encrypted, is
**recommended before anything real is enrolled** — it is not part of First
boot, because the destination and its key are things only you have: [The
off-host copy, step by step](#the-off-host-copy-step-by-step).

**The key material is not in that backup and goes off this host by hand, now**
— the OpenBao shares and root token (step 3.2), the `openbao-data`,
`its-secrets` and `its-ca` volumes, and `.env` — to a different place than the
database backups: GO-LIVE.md §6 and `docs/07-backup-restore.md` §1-2. The
`traefik-acme` volume is not key material: losing it costs one new certificate.

**First boot is complete.**

---

## Recovery link: an administrator who lost every second factor

For the one technician who lost every passkey, the authenticator app and the
recovery codes, and has no other administrator to reset them (Settings →
Users → Reset second factors), whoever holds the server prints a one-time
recovery link (design §7, Q5). Opening it removes the person's second
factors, remembered browsers and sessions, and starts a session that sets up
a new factor. **It asks for no current password: the link alone is the
account.** The recovery page always offers a new password, and requires one
when the person's password was set by an emailed link.

**It is also what to do when "Reset second factors" is refused with
`reset_needs_recovery`** (operator decision 2026-11-05). That refusal means the
person's current password was set with an emailed reset link and no second
factor has been used since: that password may be whoever holds the mailbox,
and a factor reset would hand them the account. Confirm who the person is
yourself, then give them a recovery link the way below. Opening it also clears
the refusal.

- **Hand it to the person directly** — in person, or read out over a call to
  someone you recognise. Whoever opens it before it expires gets the account;
  the command's warning says how long that is (`RECOVER_TTL`,
  `backend/app/core/auth/action_tokens.py`).
- **Never send it by email**, chat or ticket: the second factor is never
  restored by mail alone (Q5, "nunca solo por correo").
- The holder is told by mail that a link was issued, and the issue is audited
  as `user.recovery_link_issued` (never the link itself).

First, by hand, set `RECOVER` to the address the person signs in with — for
example `RECOVER=ana@novieri.com` — then, from the directory holding
`docker-compose.yml`:

```sh
: "${RECOVER:?set RECOVER to the address the person signs in with first}"
docker compose exec api python -m app.core.auth.recover --email "$RECOVER"
```

The link is printed on stdout and the same warning on stderr. It needs
`ITS_PUBLIC_CONSOLE_URL` and `ITS_DEFAULT_ORG_SLUG`; without either it prints
`refused: ...` and exits 1.

## Sign in with Microsoft (own authentication, phase 2)

In Microsoft Entra ID, register **one** application for this installation:

1. **Multi-tenant**, for work and school accounts of any organization: the
   platform signs in through the `organizations` authority ("Only users with
   work or school accounts from Microsoft Entra ID can sign in to the
   application",
   https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc).
   Technicians come from Novieri's tenant and contacts from their clients'
   tenants; the platform checks the tenant itself. **A guest (B2B) in
   Novieri's tenant does not sign in as a technician with Microsoft** -- the
   token's tenant is their home one; they sign in with their password.
2. **A web application** whose redirect URI is the console's address
   followed by `/sign-in/microsoft`. Settings -> Microsoft sign-in shows the
   exact value to paste, built from `ITS_PUBLIC_CONSOLE_URL`.
3. **No client secret.** The platform proves itself with a certificate it
   makes (`private_key_jwt`). Microsoft: "Don't use password credentials,
   also known as secrets"
   (https://learn.microsoft.com/en-us/entra/identity-platform/security-best-practices-for-app-registration).
   **The certificate is self-signed because the operator chose that
   (decision C4, 2026-11-06), knowing that Microsoft prefers one from a
   certificate authority.** Its words, on the same page: "use a certificate
   from a trusted certificate authority (CA) instead of a self-signed
   certificate. Configure a policy to enforce that certificates come from
   trusted issuers. However, if using a trusted CA isn't possible,
   self-signed certificates are still preferred over passwords." And on
   another: "in production, you should purchase a certificate signed by a
   well-known certificate authority"
   (https://learn.microsoft.com/en-us/entra/identity-platform/how-to-add-credentials).
   **If Novieri's tenant has an application management policy that only
   accepts certificates from trusted issuers (`trustedCertificateAuthority`),
   or that limits certificate lifetime to less than 360 days
   (`asymmetricKeyLifetime`), Entra refuses to upload this certificate**
   (https://learn.microsoft.com/en-us/graph/api/resources/keycredentialconfiguration?view=graph-rest-beta),
   and the platform has no way to use one issued by a CA instead. Check the
   tenant's policy before you start.
4. The platform asks for `openid profile` and nothing else.

Then, in the console, **Settings -> Microsoft sign-in** (`orgs.manage` on
every client): the application (client) ID and Novieri's tenant ID -- the
only tenant technicians may sign in from --, then **Make a new certificate
when I save**, save, and upload the certificate the tab shows (or its
download, `novieri-sign-in.cer`) to the application: *Certificates & secrets*
-> *Certificates* -> *Upload certificate*. Then switch sign-in with Microsoft
on. Each client's tenants are on that client's **Edit** form.

**The certificate lasts 360 days.** The tab shows when it expires and warns
30 days before; nothing else tells anyone, so put the date in your calendar.
On the day it expires nobody can start a sign-in with Microsoft. **Renewing
is two steps and leaves no gap:**

1. Tick *Make a new certificate when I save* and save. The new certificate
   **waits** beside the one in use, which keeps signing people in; the tab
   shows both, each with its expiry.
2. Upload the new one (or its download, `novieri-sign-in-new.cer`) to the same
   application, next to the old one -- Entra keeps several at once -- and
   then press *I uploaded it: switch to the new certificate*. From that
   moment the platform signs with the new key and destroys the old one;
   remove the old certificate from Entra afterwards.

Switching before the upload is the only way a renewal breaks sign-in: until
the new certificate is in Entra, every sign-in with Microsoft fails. Clearing
the application ID (with sign-in switched off) destroys both certificates
and their keys.

**Changing the application ID** needs a new certificate in the same save
(the tab says so beside the ID), and **from that save on nobody signs in with
Microsoft until the new certificate is uploaded to the new application**:
there is no certificate Entra already holds for it to keep signing with. Do
it at a quiet time. The very first certificate is the same, which is why the
order above is save, upload, and only then switch sign-in on.

Nothing is added to `.env` and nothing to OpenBao: the private key is a
vault credential sealed by the organization's envelope key, like the SMTP
password, decrypted only to sign and audited every time
(`auth.entra_credential_used`). The API already reaches
`login.microsoftonline.com` (Email intake). Depending on each tenant's
policy, its administrator may have to approve the application once.

Nobody gets an account by signing in with Microsoft. A person links their
Microsoft account from their invitation or from Sign-in and security; an
account nobody linked is refused. **A technician who accepts with Microsoft
also chooses a password**, so another administrator's *Reset factors* and
the recovery command always have a password to come back to.

**Passkeys for contacts must be discoverable.** A contact signs in with a
passkey without typing an address, which needs a credential stored on the
authenticator; an older security key without that storage cannot be a
contact's passkey (the browser refuses to make one).

**Without mail configured**, inviting a contact or resetting their access
shows the technician the one-time link once. That technician could follow
it themselves -- as they could by changing the contact's address and
inviting -- which the audit log records (`link_shown: true`). Configure
Settings -> Email so links go to the contact.

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
- the sign-in pages and the account emails (invitations, password resets),
  which the platform serves and sends itself: the emails in the person's own
  language, the sign-in pages in this browser's last choice or, failing that,
  the browser's own language.

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

## Sending account mail through Microsoft 365 (Settings → Email)

Microsoft is retiring password sign-in (basic auth) for SMTP. **Settings →
Email → Sign-in: Microsoft 365** makes Novieri RMM sign in to `smtp.office365.com`
with an Azure application instead. **Since own authentication (phase 1)** the
client secret is stored in Novieri RMM's vault (Settings → Email), encrypted and
decrypted only to send; Novieri RMM fetches the Microsoft token itself
(client-credentials grant) and sends the mail. The secret is never shown back.

> ⚠ **UNVERIFIED against a live Microsoft tenant.** The permission, the scope
> and the Exchange commands below follow Microsoft's documentation
> (*Authenticate an IMAP, POP or SMTP connection using OAuth*, revision of
> 2025-10-17). Everything Novieri RMM sends is tested against a fake. Until steps 9
> and 10 have been run, this is "shipped, not verified".

**Use a separate Azure application for this**, not the one Email Intake uses:
this secret sends the platform's mail, and it should not also unlock reading
mail.

Azure portal (Microsoft Entra admin center):

1. **App registrations → New registration.** Name it `Novieri RMM outgoing mail`,
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
6. **Enterprise applications** (not App registrations) → open `Novieri RMM outgoing
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
$Mailbox  = Read-Host "The mailbox Novieri RMM sends as (the From address)"
$Sp = New-ServicePrincipal -AppId $AppId -ObjectId $ObjectId -DisplayName "Novieri RMM outgoing mail"
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

In Novieri RMM:

7. **Settings → Email.** Mail server `smtp.office365.com`, port `587`,
   Encryption **StartTLS · port 587**, From address = the mailbox above.
8. **Sign-in: Microsoft 365.** Username = the same mailbox, then the
   Directory (tenant) ID, the Application (client) ID and the client secret.
   Leave *Advanced* alone. **Save.** If a password was saved before, the
   console tells you it is being replaced; it is not kept.
9. Press **Send a test message** and confirm it **arrives** in that mailbox.
   Accepted is not delivered: look in the inbox, not at the button.
10. Invite a test user and confirm the invitation arrives.

If step 9 fails with a Microsoft sign-in error, check in this order: admin
consent granted (step 4), the Enterprise application Object ID (step 6), SMTP
AUTH enabled for the mailbox, and the client secret not expired.

**Going back to a password:** choose **Sign-in: Password**, type the password,
Save. The Microsoft application details are cleared in the same save, and its
client secret is deleted from Novieri RMM's vault.

---

## Notification email channels over Microsoft 365

Ticket and platform notifications are sent by Novieri RMM itself, per organisation,
through **Notifications → Channels**. An email channel offers the same
**Sign-in: Microsoft 365**: the directory (tenant) ID, application (client) ID
and client secret go to Novieri RMM's vault (never shown again), each send fetches a
token and signs in to `smtp.office365.com:587` with StartTLS, and every use is
in the audit log as `notification.credential_used`. Password sign-in stays for
any other mail server.

The Azure side is steps 1-6 of the section above. The `Novieri RMM outgoing mail`
application can be reused; to send from another mailbox, run the
`Add-MailboxPermission` and `Add-RecipientPermission` lines again with that
mailbox. The channel's **From address** is the mailbox it sends as. Press
**Test** on the channel: the test message goes to the From address itself.
Unverified against a live tenant, as above.

---

## Email intake from a Microsoft 365 mailbox

Exchange Online refuses IMAP with a password, so a Microsoft 365 support
mailbox signs in with **Connect with Microsoft**: a person signs in once as the
mailbox, and Novieri RMM keeps the resulting refresh token in the vault and turns it
into a short-lived access token for each poll. Password IMAP keeps working
for every other mail server.

> ⚠ **UNVERIFIED against a live Microsoft tenant.** Everything Novieri RMM sends is
> tested against a fake token endpoint and a scripted IMAP server. Until steps
> 12 to 15 below have been run, this is "shipped, not verified".

**Before anything else**, `ITS_PUBLIC_CONSOLE_URL` must be set in `.env` to
the https address people open the console at (for this deployment,
`https://rmm.novieri.com`). It is the address Microsoft returns the browser
to. Without it the console says Connect with Microsoft is switched off.

**Use a separate Azure application from the outgoing-mail one** ([Sending
account mail through Microsoft
365](#sending-account-mail-through-microsoft-365-settings--email)), so that
the secret that sends mail cannot also read it.

Azure portal (Microsoft Entra admin center):

1. **App registrations → New registration.** Name it `Novieri RMM email intake`,
   choose *Accounts in this organizational directory only*.
2. Under *Redirect URI* choose **Web** and enter the address the console shows
   under Email intake → **Microsoft 365 connection → Return address to
   register in Azure**. For this deployment that is
   `https://rmm.novieri.com/settings/email-intake/oauth/callback`. Press
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
    support mailbox, open **Apps**, and remove *Novieri RMM email intake*. Within one
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
Reconnect. Novieri RMM uses the token every poll, so it does not lapse from disuse
while the mailbox is switched on.

---

## The email-domain allowlist (`ITS_CONSOLE_EMAIL_DOMAIN`)

Refuses any token whose `email` claim is not at your domain. Unset — which is
the shipped default — means **no restriction**, exactly today's behaviour.

Allowed iff the domain equals the configured base or ends with `.` + the base,
both lowercased. `you@novieri.com` and `you@mail.novieri.com` pass;
`you@evilnovieri.com` fails (no dot) and `you@novieri.com.evil.com` fails
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
the only way to learn the cost before paying it. Run it from `deploy/`, in a
one-off `api` container: that container already holds the admin URL
(`ITS_DATABASE_ADMIN_URL` in `.env`) and reaches Postgres, which publishes
no port on the host. The script is mounted from the checkout; the domain is
the one you mean to set:

```sh
docker compose run --rm --no-deps --label traefik.enable=false \
  -v "$PWD/../scripts/preflight_email_domain.py:/srv/app/preflight_email_domain.py:ro" \
  --entrypoint python api preflight_email_domain.py novieri.com
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
| should keep access | No screen changes a technician's address (none has since own authentication phase 1): delete the account and invite the correct address (**Settings → Users**), or disable it (next row). |
| should not keep access | Disable them: console → **Users → Disable**, i.e. `POST /api/v1/users/{id}/disable`. The preflight lists non-active rows but they do **not** set the exit code, and the same gate refuses a disabled user regardless of domain |

Re-run until it exits `0`. If it still exits `1` on an address you have
already fixed, that is the divergence in §4 below, not a failed fix.

### 2. Then set it, and restart

```
ITS_CONSOLE_EMAIL_DOMAIN=novieri.com
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

Confirm the api no longer holds the rule, before doing anything else:

```sh
docker compose exec api python -c "from app.core.config import get_settings; print(repr(get_settings().console_email_domain))"
```

`''` is recovered: sign in at `https://rmm.novieri.com/` and the console opens.
A refusal after that means something other than the allowlist is refusing you.

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
  "since their last sign-in": an address changed this morning by
  somebody who has not logged in since is still the old one here, in both
  directions. A row that reads clean can belong to someone whose live claim now
  fails, and a row that reads dirty can belong to someone the gate would now
  admit. Treat exit `0` as evidence about the rows as of each user's last
  sign-in, not a proof about the tokens they will present next.
- **It is not a replacement for disabling an account.** The same gate refuses
  any user whose Novieri RMM `status` is not `active`, and that is the control for
  "this person has left" — a domain rule that happens to exclude them is a
  coincidence, not a policy.

## AI connections (MCP)

An AI app — Claude, or any client that speaks the Model Context Protocol —
can use Novieri RMM **as the person who connected it**: it sees what that person
sees and can do what their role allows, every call is in the audit log
(`mcp.tool_called`), and the app's own settings decide which tools need the
person's approval. Approving an app is audited too (`mcp.app_authorized`); a
call over MCP is not a sign-in, so the person's last sign-in stays the last
time they signed in to the console. The server is `/mcp` on the `api`
container, and the platform is its own OAuth 2.1 authorization server; there
is no new service. Design: `docs/superpowers/specs/2026-10-09-mcp-server-design.md`,
and for the OAuth server `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §5.

**It is off by default, three times over.** No AI app connects until all
three hold:

1. the deployment has a public https address **with no path**
   (`ITS_PUBLIC_CONSOLE_URL`) — without one, `/mcp` answers 503; with a path
   after the host (`https://example.com/itms`), too, because an OAuth issuer is
   an https address with no path, so the platform has none and says so;
2. an org-admin has turned on **Settings → AI connections → Allow AI apps to
   connect over MCP** for the organization;
3. the person holds the `mcp.use` permission.

The second and third are checked on every call, not only at connection time,
and the consent page checks them first: while either is missing, it reads no
app's metadata document and writes nothing.

**How an app introduces itself.** Claude and Claude Code publish a *client
metadata document* at an https address of their own; the platform reads it
when a signed-in person opens the consent page, and that needs no further
setting (operator decision 2026-11-06). An app that can only *register
itself* (OAuth dynamic client registration) needs the operator's opt-in,
`ITS_MCP_REGISTRATION=open`. Without it the platform refuses every
registration and does not advertise the endpoint; with it, registration is
still refused while MCP is off for the organization, and the platform holds
at most 200 live registrations. Closing registration again stops new apps
registering; an app that registered while it was open keeps its registration
until it goes unused for 30 days, when the daily sweep removes it.

### Turning it on

1. **`ITS_PUBLIC_CONSOLE_URL` must be set in `.env`** to the https address
   people open the console at, with no path (for this deployment,
   `https://rmm.novieri.com`). It may already be: the console's sign-in,
   Email intake and the install link use the same setting. Check with:

   ```sh
   grep '^ITS_PUBLIC_CONSOLE_URL=' .env
   ```

   If that prints nothing or an empty value, edit the line in `.env` by hand
   so it reads `ITS_PUBLIC_CONSOLE_URL=` followed by your address. Keep a
   single line: a second `ITS_PUBLIC_CONSOLE_URL=` further down silently wins.
   The MCP address is this value plus `/mcp`, and the platform's tokens are
   issued for exactly that address, so do not add a trailing path.
2. **Deploy** as in "Upgrading an existing deployment". The `api` container
   reads the setting when it starts.
3. **Check it.** The platform's OAuth metadata names the console as its
   issuer:

   ```sh
   curl -s "$(sed -n 's/^ITS_PUBLIC_CONSOLE_URL=//p' .env)/.well-known/oauth-authorization-server"
   ```

   The answer is JSON whose `issuer` is your console address and which carries
   `"client_id_metadata_document_supported": true`. It carries
   `registration_endpoint` only when you opted in to registration (step 7)
   and MCP is on. Only that JSON is the `api` answering: the console's
   catch-all router answers any path the `api` does not take with its own
   HTML page and a 200. That page
   comes back while the `api` container is still starting -- Traefik leaves
   out a container whose health check has not passed yet, so wait a minute
   and run it again -- and it is what a path after the host in step 1 gives
   for good. With `http://` nothing answers at all, and a 503 with
   `temporarily_unavailable` is the `api` container saying that
   `ITS_PUBLIC_CONSOLE_URL` is not an https address without a path.
4. **Settings → AI connections:** an org-admin turns on **Allow AI apps to
   connect over MCP**. The tab shows the connection address.
5. **Settings → Roles:** grant `mcp.use` to the roles that may connect.
   Org-admins hold `mcp.use` by default; nobody else does until you grant it.
6. **Connect an AI app.** In the app (in Claude: add a custom connector),
   give it the connection address. The app opens the console's consent page;
   the person signs in to the console first if they are not (password and
   second factor, as always). The page is titled by the host the app cannot
   disguise — its metadata document's, or the one its answer goes back to —
   and shows the name the app gave itself as the app's own claim; for an app
   that only answers on the person's own computer, it says so. **Approving
   needs the person's second factor confirmed in the last ten minutes**, on a
   remembered browser too, and the person then receives a mail, "You
   connected <host>" (operator decision 2026-11-06), when account mail is
   configured in Settings → Email; without it the approval still succeeds
   and nothing is mailed. Approving lets the app act as the person. Then, in
   the app's settings for that connector, set each tool to *Always allow* or
   *Needs approval*; tools that change something are safest on *Needs
   approval*.
7. **Only for an app that cannot publish a metadata document:** opt in to
   registration. Read "Residual risks, stated plainly" below first: risk 3
   exists only while it is set. From the directory holding
   `docker-compose.yml` and `.env`, this removes any earlier
   `ITS_MCP_REGISTRATION` line and adds the one that opens registration:

   ```sh
   sed -i '/^ITS_MCP_REGISTRATION=/d' .env && echo 'ITS_MCP_REGISTRATION=open' >> .env
   ```

   Only the exact word `open` opens it; any other value, or no line at all,
   keeps it closed. Deploy again for the `api` container to read it.

The consent page, like every console page, is never shown inside another
site's frame: nginx answers every console request with
`Content-Security-Policy: frame-ancestors 'none'` and `X-Frame-Options: DENY`
(`deploy/nginx.conf`, held by `backend/tests/test_deploy_frame_headers.py`), so
a page elsewhere cannot overlay it and trick a click on **Approve**.

### What Traefik routes

| Path | Goes to |
|---|---|
| `/mcp` | `api` — the MCP endpoint |
| `/.well-known/oauth-protected-resource`, `…/mcp` | `api` — tells an app that the platform is its authorization server |
| `/.well-known/oauth-authorization-server` | `api` — the platform's OAuth metadata (RFC 8414) |
| `/.well-known/jwks.json` | `api` — the public halves of the platform's signing keys (own authentication phase 1); AI apps do not need it, the platform verifies its own tokens |
| `/oauth/token`, `/oauth/register`, `/oauth/revoke`, each by exact path | `api` — the platform's OAuth server |
| `/oauth/authorize` | the console — the consent page, behind the console's own sign-in |
| `/.well-known/openid-configuration`, and either document with a path after it | `api`, which answers 404 on purpose: apps look there first and move on after a 404, but give up on the console's 200 page |
| `/realms/…`, `/resources/…`, `/js/…`, `/admin/…` | the console's own page: no router names them since own authentication phase 4, so no identity provider is reachable on `:443` |

The `deploy` CI job proves this live, through the real Traefik, `web` and
`api` containers and the api on a real OpenBao (`deploy/ci/mcp_flow.py`):
the 401 and its `resource_metadata`, both discovery documents, the 404 where
an OpenID document would be, registration closed and then open, the consent,
PKCE, the token's audience, refresh rotation and reuse, RFC 7009, `/mcp`
itself, and that the old identity provider's paths reach only the console.
The backend suite proves the server's decisions
(`backend/tests/test_oauth_*.py`, `test_mcp_own_tokens.py`).

### Revoking

- **One app:** Settings → AI connections lists every app that called a tool
  here in the last 90 days **and every app that holds a live grant** — yours,
  or anyone's if you hold `orgs.manage` — so one approved a moment ago is
  there before it has done anything. **Revoke for me**, or, for an
  org-admin, **Revoke for everyone**, which needs both `mcp.use` and
  `orgs.manage`, and a view of every client (an org-admin has all three).
  The app's grants end — yours, or everyone's in this organization — in the
  same transaction as the record of the revocation, and so do the codes it
  was given and has not exchanged yet: every token the app holds stops at
  its next call, and its refresh tokens mint nothing. No identity provider is
  involved, so there is no second half that can fail. It never affects
  another organization. An app listed without a **Revoke** button last acted
  here before it registered with the platform, or its registration expired:
  it holds no grant here.
- **Revoking is not a block-list.** It ends the grants the app holds now; it
  does not stop the app coming back. A person can approve the same app again,
  and an app can publish another metadata document, or (with the opt-in)
  register again, under any name it likes. To keep AI apps out, use one of
  the stops below.
- **One person:** take `mcp.use` away from their role. Their next call is
  refused. Disabling or deleting the person, resetting their factors (by an
  administrator, or with a recovery link), a password change (their own, or
  through a reset mail), the person's own **Sign out everywhere** (their
  security page, `POST /api/v1/auth/logout-everywhere`) and, since
  2026-11-15, an administrator's **Sign out everywhere** (Settings → Users)
  end all their AI grants too, and the codes an app has not exchanged yet;
  the act's own audit event says how many grants ended
  (`ai_grants_revoked`). An administrator's sign-out still leaves the
  person's remembered browsers as they are.
- **The whole organization — the emergency stop:** Settings → AI
  connections → turn **Allow AI apps to connect over MCP** off. Every app is
  refused from its next call, whatever it is. While it is off, apps can
  still refresh their tokens (`/oauth/token` does not read the switch), but
  every call they make is refused. **Turning it back on ends no grant and
  restores every one that is still live:** the apps connected before work
  again at once. To end those grants, so that people approve the apps
  again, revoke the apps for everyone before turning the switch back on.
- **New apps that register themselves, deployment-wide:** delete the
  `ITS_MCP_REGISTRATION=` line from `.env` and redeploy. The platform refuses
  every new registration; apps already registered, and apps known by a
  metadata document, are unaffected, so pair it with one of the stops above
  or below.
- **The whole deployment — the deployment-wide stop:** empty
  `ITS_PUBLIC_CONSOLE_URL` and redeploy. `/mcp` answers 503 and the platform
  has no OAuth issuer. This also turns off everything else that uses the
  address — the console's own sign-in and passkeys, the links in the
  platform's account mail, push install, the Group Policy MSI, Email
  intake's Microsoft sign-in, the install link (`.env.example` lists them
  under `ITS_PUBLIC_CONSOLE_URL`) — so it is a last resort, not a switch.

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
- the first-run setup claim;
- approving an AI app on the console's consent page, whose one-time code
  becomes the person's MCP tokens: an app can never approve itself or
  another.

**Not tools:** files and streams — the install-link MSI, every CSV and XLSX
export, recordings, document contents, attachments (download and upload) —
and the in-app AI assistant's chat. These are not refused by the API a
second time, and do not need to be: **every MCP tool call can reach only its
own route.** The API refuses a tool's request unless the router would send it
to the exact route that tool was generated from (`app/mcp/guard.py`), so no
tool can be steered at a route that is not one.

**The acts that ask for your second factor again** (own authentication phase
2): saving Settings → Email, Sessions or Microsoft sign-in, changing a
client's Microsoft tenants, inviting a technician or sending the invitation
again, granting or removing a role, re-enabling a disabled person, creating a
role, changing a role's permissions or deleting a role, and switching AI
connections on need the person's second factor confirmed in the console in
the last ten minutes, and **an AI app connected over MCP cannot do any of
them** -- the settings, the invitations, the grants, re-enabling and the
role definitions are not tools and are refused on the channel; a client edit
and the organization's settings stay tools and refuse that part. Cancelling
an invitation, disabling a person, signing them out, switching MCP off,
revoking an app and renaming a role never ask.

Every result is also scanned for secret-looking field names, at every depth,
and one hit blocks the whole result rather than redacting it.

### Residual risks, stated plainly

**Risk 3 exists only while self-registration is open**
(`ITS_MCP_REGISTRATION=open`); closing it ends it for new apps. **Risks 1, 2,
4, 8 and 9 exist whenever MCP is on**, because an app known by its client
metadata document needs no opt-in. Risks 5, 6, 7 and 10 hold always.

1. **Consent phishing.** Anyone can publish a client metadata document — or,
   with the opt-in, register an app — under any name, and send a person a
   link to the consent page; that page is the only check between the link
   and the person's full authority. It is titled by the host the app cannot
   disguise (its document's, or the one its answer goes back to; ASCII only,
   so no look-alike letters), shows the app's chosen name only as its own
   claim, says what approving hands over — "If you approve, it can act as you
   in Novieri RMM: everything your role allows, including running actions on managed
   computers" — needs the person's second factor confirmed in the last ten
   minutes, and mails them "You connected <host>" afterwards when account
   mail is configured in Settings → Email (without it the approval still
   succeeds and nothing is mailed). Org-admins should approve only apps they
   connected themselves, and anyone who gets that mail for an app they do
   not recognise should revoke it.
2. **An app known by its client metadata document makes the platform fetch
   that address**, only inside a signed-in person's consent, with MCP on and
   `mcp.use` held. The fetch goes through the platform's outbound guard (only
   public addresses — no private, loopback, link-local or other special-use
   ones (RFC 6890) — port 443 only, no redirects), is cut at 5 KiB and 5
   seconds, and its answer is cached as its own cache headers say, bounded to
   between 5 minutes and 24 hours. A document that is refused is never
   stored; one that cannot be read just now keeps the copy already stored.
   A signed-in person can still make the `api` container send one bounded
   request to a public address of their choosing each time it needs the
   document. Refusals are logged at most once a minute.
3. **Registrations can be made by strangers** (only with the opt-in).
   Registering needs no account. One network may register 20 apps an hour
   (an IPv6 address is counted by its /64), registration is refused while MCP
   is off, and the platform holds at most 200 live registrations, after which
   it answers 503 until unused ones expire; a registration nobody uses for 30
   days goes with the daily sweep, which audits it, unless a new
   registration purges it first, which does not. Nothing is exposed by it —
   registering grants nothing without a person's consent. The hosted Claude
   apps do not register: they use their metadata documents. Their requests come from
   Anthropic's published egress ranges (`160.79.104.0/21` and
   `153.61.208.0/20`), not from your office. Every Claude user shares those
   addresses, and Anthropic does not say how many of them it uses, so one
   address can carry many people's requests; that is why the token and
   revocation endpoints together allow 600 requests a minute per address (an
   IPv6 address is counted by its /64).
4. **http redirects are allowed only to the person's own computer**
   (`127.0.0.1`, `[::1]`, `localhost`, any port but 0, which is refused),
   which desktop AI apps use (Claude Code's metadata document among them).
   Whatever answers on that port of the person's computer receives the
   authorization code; it is bound to the app's PKCE verifier and cannot be
   redeemed without it. The consent page says when an app only answers
   there.
5. **Free text can hold a secret under an innocent name.** The scan blocks
   results by field *name*; a password someone typed into a documentation
   record's notes field is text like any other and reaches the app as the
   person could read it.
6. **A stolen access token works for at most ten minutes, or until its grant
   ends.** It names its grant, which the API reads on every call: revoking the
   app, the person's account acts above (an administrator's "Sign out
   everywhere" among them), or the app's refresh token being used twice (a
   sign it was copied) end it at once.
7. **A refresh token used twice ends the app's grant, with no grace.** That is
   what a copied token looks like (design §5.3). It is also what an honest
   race looks like: a refresh whose answer the app never read (Claude stops
   waiting after 30 seconds), or two refreshes with the same token at once.
   Claude refreshes on two triggers, ahead of time (up to five minutes before
   the token expires) and when a call is refused with 401, so two can meet.
   Either way the app's next call is refused, and the person removes the
   connector and adds it again.
8. **One click can approve, within ten minutes of a second factor.** The
   consent page refuses to be framed (`frame-ancestors 'none'`,
   `X-Frame-Options: DENY`), but a page on another site can open it as a
   window of its own under the second click of a double click, which then
   lands on **Approve**. OWASP calls this DoubleClickjacking, and framing
   rules do not stop it. Approving needs the person's second factor
   confirmed in the last ten minutes, so outside that window the trick only
   reaches the second-factor prompt; inside it, one click is enough. It is
   bounded, not removed: the page is titled by the app's host, the person
   is mailed "You connected <host>" when account mail is configured in
   Settings → Email (without it the approval still succeeds and nothing is
   mailed), and Settings → AI connections revokes the app.
9. **A password change does not stop an approval in the session it keeps.**
   The change ends the person's AI grants and every other session of
   theirs, but an AI app approved afterwards in the session the change was
   made in — even on a consent page opened before it — still receives its
   code: it is the same person, in their own session.
10. **A used refresh token is remembered for 30 days** (operator decision
    E2, 2026-11-15): from its first use, whoever made it, until the first
    daily sweep more than 30 days later. Until then, presenting it again
    ends the app's grant (risk 7). Once that sweep has deleted it,
    presenting it is a plain `invalid_grant`, and the grant lives on. That
    costs something either way round. If someone else refreshed with a
    copied token first, the theft is caught only if the real app presents
    its own copy before that sweep — the window runs from the thief's
    refresh, not from the theft; an app that comes back later is simply
    asked to connect again, while the copy goes on working until its grant
    is revoked or sits unused for 30 days. If the real app refreshed first,
    a thief who presents the stale copy before that sweep ends the grant and
    leaves `mcp.refresh_reused` in the audit log, the sign that the token
    leaked; after it, the copy is refused like any unknown token: the thief
    gains nothing, but nothing in the audit log says the token leaked.
    Settings → AI connections lists every app holding a live grant. If an
    app asks you to connect again when you did not expect it, use **Revoke
    for me** on it there before reconnecting: that ends every grant of
    yours to that app, the copy included.

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
possible with `curl` and a hand-copied bearer token; since own authentication
phase 1 the API takes the console's session and no bearer token, so the
console is the only path (step 2).

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
release…** button, and the API refuses the publication with `403 requires
agent.release.manage`. The fix is a direct grant on the database host, from
the directory holding `docker-compose.yml` and `.env`. First, by hand, set
`PUBLISHER` to the email that account signs in with — for example
`PUBLISHER=you@novieri.com` — then:

```sh
: "${PUBLISHER:?set PUBLISHER to the email the account signs in with first}"
docker compose exec -T -e PGPASSWORD="$(grep '^POSTGRES_ADMIN_PASSWORD=' .env | cut -d= -f2-)" \
  postgres psql -U its_admin -d itsuite -v publisher="$PUBLISHER" <<'SQL'
INSERT INTO user_roles (id, org_id, user_id, role_id, client_id)
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

`client_id` is the grant's scope, and `NULL` means **every client**, including
ones created later (migration `0230` replaced the old `device_group_id` column
with it; a runbook written before then and still using `device_group_id` fails
with `column "device_group_id" of relation "user_roles" does not exist`).
`super-admin` is a platform role, so this is the grant you want.

`ON CONFLICT DO NOTHING` is now belt and braces rather than the whole guard:
migration `0073` added a partial unique index on `(user_id, role_id) WHERE
device_group_id IS NULL`, and `0230` rebuilt it as `WHERE client_id IS NULL`,
so a second run is refused at the database rather than producing the
duplicated role the console used to render. Keep the clause anyway — it turns
that refusal into `INSERT 0 0` instead of an error. (It names no conflict
target, so it matches whichever unique rule the row would break and needs no
edit when that rule's columns change.)

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
six binaries (`novieri-agent-{linux,darwin}-{amd64,arm64}`,
`novieri-agent-windows-{amd64,arm64}.exe`), `SIGNATURES`, `SHA256SUMS` and
`release-signing-key.pub`. Download them to the machine you run the console
from:

```sh
: "${VERSION:?set VERSION to the version you are releasing first, e.g. VERSION=1.4.0}"
gh release download "agent-v$VERSION" --dir "novieri-agent-$VERSION"
ls "novieri-agent-$VERSION"
```

(Or in GitHub: **Releases → the draft "novieri-agent 1.4.0" → Assets**, one click
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
2. **Choose binaries…**: select every file in `novieri-agent-$VERSION` at once.
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
again is not a 409. If the removal itself fails the result says so, and
**Release channels → Remove…** on the same page clears it — with every other
package of that version, so publish those again afterwards.

Every release is registered as **not code-signed** (`signed: false`): that
field is an Authenticode / Developer ID claim, and the workflow makes none.
Putting `true` there for a binary nobody signed would put a false word on the
screen of the operator deciding whether to trust the download.

A new release starts on the **pilot** channel.

#### No command-line fallback

The `curl` commands this section used to show sent `Authorization: Bearer
$TOKEN`, and since own authentication phase 1 the API reads the console's
session cookie and no bearer token (`backend/app/api/deps.py`, `admit`): they
answered `401`. Publish from the console, from any machine with a browser; the
VPS itself needs none. To remove a version (a wrong upload, or one you need to
clear): **Agent Packages → Release channels → Remove…**, which removes every
package of that version in one request. It is refused while an active update
rollout still uses the version.

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
cat "novieri-agent-$VERSION/SHA256SUMS"
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
`GPO`, `Intune` or `Manual`. The dialog makes two calls, described below; they take
the console's session, so neither has a `curl` form:

**a. The package — artefact, digest, staging steps. Carries no credential.**

`GET /api/v1/agent/releases/{id}/deployment-package?channel=gpo`.

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
deployment package`** means a release row exists with no bytes: an upload
that stopped between its two requests. (The console's
dialog removes such a row itself when an upload fails.) Staging an object that does not
exist fails at the far end of a copy, on somebody's file share, which is the
most expensive place to diagnose it — so it is refused here instead. A **422**
means the `channel` was not one of `gpo`, `intune`, `manual`; it is refused
rather than defaulted, because the channel decides the integrity statement
below and an unknown one falling back to `manual` would tell a GPO
administrator that the endpoint verifies before installing, which on their path
is false.

**b. The credential — on its own, with mandatory bounds.**

`POST /api/v1/agent/releases/{id}/deployment-token`, with for example
`{"max_uses":260,"expires_in_hours":72,"channel":"gpo","note":"HQ rollout"}`.

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
# Staged on your DFS share or SYSVOL folder, run by a computer startup
# script as SYSTEM.
# 1. Install the service, and do not start it:
.\install.ps1 -Binary .\novieri-agent-windows-amd64.exe -NoStart
# 2. Your own secret mechanism sets $env:ITS_AGENT_ENROLL_TOKEN here, from a
#    location only machine accounts can read. Which one is yours to choose;
#    this runbook names none.
# 3. Enrol:
.\novieri-agent-enroll.ps1 -Url https://rmm.novieri.com
```

`install.ps1` registers the service and does **not** start it when there is
nothing to enrol with; `novieri-agent-enroll.ps1` (new on 2026-08-26 — Windows had
no counterpart to the Unix `novieri-agent-enroll`) takes the token from the
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

## After a reboot or a restart

OpenBao comes back **sealed** after every host reboot and every `docker compose
up -d` that recreates it, and a sealed OpenBao makes `docker compose up -d`
**block**: `openbao-init` waits on a health check that passes only when it is
unsealed. So this sequence always starts by asking, whatever else happened
(`.env` must already hold `ITS_PUBLIC_HOST` and `ITS_ACME_EMAIL`, as it does
on this VPS from step 1: Compose interpolates the whole file for every
subcommand, `bao status` included):

```sh
cd /opt/it-suite/deploy
docker compose exec openbao bao status
```

`Sealed false`: skip to `docker compose up -d` below. `Sealed true`: three of the
five shares, each at the prompt, from three custodians (GO-LIVE.md §4):

```sh
docker compose exec openbao bao operator unseal
docker compose exec openbao bao operator unseal
docker compose exec openbao bao operator unseal
docker compose exec openbao bao status
```

`Sealed false`. Then:

```sh
docker compose up -d
docker compose ps -a --format 'table {{.Service}}\t{{.Status}}'
./scripts/check-public-tls.sh
curl -s -o /dev/null -w '%{http_code}\n' https://rmm.novieri.com/api/v1/devices
```

`up -d` runs `migrate` first, as `api`'s dependency, every time; after a reboot
it finds nothing to migrate and exits 0. The table is step 7's; the certificate
check prints `ok`; and the last line prints `401` (the API answering, no one
signed in). A `503` there means the api is not healthy yet (Traefik answers 503
for the api's paths while it is not): wait, and look at `docker compose logs
api` if it lasts.

If the optional static-key auto-unseal is on (`./scripts/openbao-seal.sh
status`, GO-LIVE.md §4), `bao status` already says `Sealed false` and the three
unseal lines are not needed.

---

## Upgrading an existing deployment

**The short version: `./scripts/deploy.sh`.** It pulls, diffs what arrived
against what the *running image* was built from, and runs only the steps that
change actually needs — building `web` alone for a frontend release, running
`migrate` only when a new revision file appeared, and so on. Run
`./scripts/deploy.sh --plan` first to see the decision before acting on it:
it pulls too, and changes nothing else.

**Any other existing host** — one whose `.env` predates the console's host and
ACME keys — needs both before the first `docker compose` command of any
sequence, `docker compose exec openbao bao status` included: Compose
interpolates the whole file for every subcommand and stops at `set in .env`
when either is missing. (On the VPS this runbook builds, `new-env.sh` wrote
them in step 1, so nothing here applies.) Add them by hand and **never run
`new-env.sh`**, which would write a fresh random password into every database
role and refuse to overwrite the file anyway. Each is its own block, and each
deletes any earlier line first, since a second `KEY=` line in `.env` silently
wins over the first. The host is computed from `ITS_PUBLIC_CONSOLE_URL`, which
every host already has and which is exactly what the browser's address bar
shows:

```sh
host="$(sed -n 's|^ITS_PUBLIC_CONSOLE_URL=https://\([^/:]*\).*|\1|p' .env | tail -1)"; if [ -n "$host" ]; then sed -i '/^ITS_PUBLIC_HOST=/d' .env && printf 'ITS_PUBLIC_HOST=%s\n' "$host" >> .env && grep '^ITS_PUBLIC_HOST=' .env; else echo "REFUSED: ITS_PUBLIC_CONSOLE_URL in .env is not an https:// address; set it first" >&2; fi; unset host
```

The contact address Let's Encrypt is given, at a prompt (a prompt ends its
block):

```sh
read -rp 'Let'"'"'s Encrypt contact address: ' E && [ -n "$E" ] && sed -i '/^ITS_ACME_EMAIL=/d' .env && printf 'ITS_ACME_EMAIL=%s\n' "$E" >> .env; unset E
```

**On the VPS this runbook builds**, an upgrade is these commands, in this order
(the operator always deploys from `main`). First, alone:

```sh
cd /opt/it-suite/deploy
docker compose exec openbao bao status
```

`Sealed true` means unsealing as in [After a reboot or a
restart](#after-a-reboot-or-a-restart) before anything else, since `deploy.sh`
refuses to start on a sealed OpenBao. `Sealed false`:

```sh
git -C .. pull --ff-only
./scripts/deploy.sh --plan
./scripts/deploy.sh
```

**Not on `itms.toeshee.com`, the host this VPS replaces.** Until that host is
switched off, do not `git pull` or run `deploy.sh` there from this `main`: its
`.env` has no `ITS_PUBLIC_HOST` or `ITS_ACME_EMAIL`, so `docker compose`
refuses to start, and with them added by hand Traefik would stop serving the
certificate copied there and ask Let's Encrypt for one.

`--plan` pulls as well (after the line above, a no-op) and shows what the
upgrade will run, without running any of it; `deploy.sh` then
takes a backup, builds the images tagged with the new commit, migrates (`migrate` also runs on every
`up -d`), restarts what changed, and verifies: the tag is HEAD, the API answers
`401`, and the certificate on `:443` passes `check-public-tls.sh`. It ends by
printing the way back from this upgrade.

**The first upgrade past own authentication phase 4 (2026-11-15) starts with
the pull**, from `deploy/`:

```sh
git -C .. pull --ff-only && ./scripts/deploy.sh
```

The release before it runs its own `deploy.sh`, which pulls part-way through
-- and bash goes on running the text it already opened, which restarts and
provisions the old identity provider, services this compose file no longer
has: it stops after the migration, with the app tier still on the old images.
If that already happened, run `./scripts/deploy.sh` again: it sees the old
identity provider's containers still there and finishes the upgrade as a full
deploy, instead of answering "Already deployed".

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
- **fails on any one-shot** (`migrate`, `postgres-grants`, `openbao-init`)
  that exits non-zero;
- **reports the nightly backup timer**, or says how to install it
  ([Platform backups](#platform-backups-nightly-off-host-and-a-monthly-drill)).

Since 2026-11-15 (own authentication phase 4) it also:

- **removes the old identity provider's two containers, `keycloak` and
  `keycloak-init`**, once the new app tier is up: compose leaves a container
  whose service left the file running, and Traefik would go on routing to
  it. It finds them by the labels Compose put on them -- this project's
  `com.docker.compose.project` and those two services'
  `com.docker.compose.service` -- and nothing else in the project is
  touched. Its Verify step fails while one is left, and a `docker` that
  does not answer stops the run;
- **keeps the old identity provider's database in the backup it takes before
  an upgrade** (`ITS_BACKUP_OLD_IDENTITY_DB=1`, which step 1 of the manual
  sequence below sets too); the nightly backups leave it out;
- **names the old identity provider's `.env` keys and says to keep them**
  until [Retiring the old identity provider's
  data](#retiring-the-old-identity-providers-data).

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
#    ITS_BACKUP_OLD_IDENTITY_DB=1: the backup before an upgrade keeps the old
#    identity provider's database while one exists, as deploy.sh's does (a
#    backup.sh from before own authentication phase 4 ignores it, and dumped
#    it anyway).
cd deploy && ITS_BACKUP_OLD_IDENTITY_DB=1 ./scripts/backup.sh ~/backups/pre-upgrade-$(date -u +%FT%H%MZ) && cd ..
#    That writes globals.sql alongside the dumps, and globals.sql CONTAINS
#    THE ROLES' SCRAM PASSWORD HASHES. It is mode 600, but the directory is
#    still a secret-bearing directory: encrypt it before it leaves the host
#    (docs/07-backup-restore.md §2.1 and §2.4) and do not park it on a share.

# 2. Pull. `.env` is gitignored, so it is not touched.
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

# 6. Restart the application services.
#    THIS IS ALSO HOW CELERY BEAT PICKS UP A NEW SCHEDULE. Beat is not its own
#    container -- the `worker` service runs `celery ... worker --beat`, so the
#    beat schedule is read once, at worker start, from `app/worker.py`. A
#    release that adds a beat entry does nothing at all until the worker
#    container is RECREATED, and `up -d` recreates it only because step 3
#    changed IT_SUITE_VERSION. If you ever skip step 3, force it:
#      docker compose up -d --force-recreate worker
docker compose up -d

# 6b. The old identity provider's two containers go: compose leaves them
#     running and Traefik routes to them. Found as deploy.sh finds them, by
#     the labels Compose put on them (this project, and those two
#     services), not by `compose ps`; nothing else is touched. The first
#     line prints this project's name (it-suite unless .env sets
#     COMPOSE_PROJECT_NAME); the loop prints the id of each container it
#     removed, and nothing when none is left. Needed only on the upgrade
#     past own authentication phase 4, and a no-op after it:
project="$(docker compose config --no-interpolate | awk '/^name: / { print $2; exit }')"; echo "project: $project"
for service in keycloak keycloak-init; do
  docker ps -aq --filter "label=com.docker.compose.project=${project:?docker compose config named no project}" \
    --filter "label=com.docker.compose.service=$service"
done | xargs -r docker rm -f

# 7. Verify.
docker compose ps -a --format 'table {{.Service}}\t{{.Status}}'
docker compose exec -T postgres \
  psql -U its_admin -d itsuite -tAc 'select version_num from alembic_version;'
curl -o /dev/null -w '%{http_code}\n' https://rmm.novieri.com/api/v1/devices   # 401
#    And that the running worker really holds the new beat schedule -- this
#    reads it out of the container that is actually running, not out of the
#    source tree you pulled.
docker compose exec worker python -c \
  "from app.worker import create_celery; print('\n'.join(sorted(create_celery().conf.beat_schedule)))"
```

⚠ **PARTLY UNVERIFIED as a sequence.** Every individual command here was run
during this first boot, and steps 3–7 were run repeatedly against a live
stack as fixes landed -- except what own authentication phase 4
(2026-11-15) rewrote or added, which no live host has run: step 1's
`ITS_BACKUP_OLD_IDENTITY_DB=1` and step 6b here, and First boot's step 3.5
token writes, step 8 password field, step 9 sign-in and steps 11c and 11d.
A true `git pull`-over-an-older-release upgrade has still not been
performed on a live host, because this is the first deployment there is.

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
- A rollback past own authentication phase 4 on a live host. CI's `deploy`
  job restores a pre-upgrade backup that holds `keycloak.dump` and checks
  the database came back; nothing has started the release before phase 4
  on it since.
- The removal of the old identity provider's containers on a live host.
  The label query `deploy.sh` makes, and step 6b above, have been run only
  against a simulated `docker`, never against containers a release before
  phase 4 left behind.

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
| `/var/lib/it-suite/backup-status/` | `backup.json`, `offsite.json`, `drill.json`: the results Novieri RMM reads (mounted read-only into `api` and `worker`) |

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
own volumes, passwords generated per run. It proves the dumps restore, the
RLS policy count and `app_user` narrowing (via `restore.sh`), the schema
revision, the audit-chain linkage, and, for a backup that holds
`keycloak.dump` (one taken before own authentication phase 4, or by
`deploy.sh` before an upgrade), that the restored `keycloak` database holds
tables. It does not prove the way back past phase 4: the nightly backups it
drills leave that database out, and that way back is only the backup taken
before the phase-4 upgrade ([Rolling back a
deployment](#rolling-back-a-deployment)). It does **not** cover key material: `openbao-data`,
`its-secrets`, `its-ca`, the OpenBao shares or unseal key file and `.env`
are still backed up by hand, **to a different place than the
database backups** (`docs/07-backup-restore.md` §1-2).

Run a drill by hand at any time: `sudo ./scripts/restore-drill.sh`
(`--keep` leaves it up to inspect).

### The off-host copy, step by step

**Recommended before anything real is enrolled; not part of First boot.**
Until it is set, every backup is on this VPS (`OFFSITE=none`), and a backup
on the VPS goes with the VPS: lose the machine or its disk and there is
nothing to restore — and on a new host, restoring a backup is the only way
back ([Rolling back a deployment](#rolling-back-a-deployment)). These steps
send each night's backup to a machine or a bucket you choose, the database
part encrypted. ⚠ **UNVERIFIED** in this repository's rehearsal, which had
no off-host target; they set the keys `scripts/scheduled-backup.sh` reads.

**1. The encryption key, on your own computer — never on the VPS.** The
database backup holds role password hashes, so it leaves the host encrypted
to an `age` key whose private half the VPS never holds. On your computer,
with `age` installed, run `age-keygen -o novieri-rmm-offsite.agekey`: it
prints `Public key: age1…`. Keep that file with the OpenBao shares (GO-LIVE.md
§6): without it, the off-host copy cannot be read.

**2. On the VPS, `age` and the public key** (paste the `age1…` line at the
prompt):

```sh
sudo apt-get install -y age
```

```sh
read -rp 'age public key of the off-host copy (age1...): ' R && sudo sed -i "s|^OFFSITE_AGE_RECIPIENT=.*|OFFSITE_AGE_RECIPIENT=$R|" /etc/it-suite/backup.conf; unset R
```

**3a. To a machine you reach over SSH (rsync).** A key for the copy alone,
whose public half you authorise on the destination, for that one directory:

```sh
sudo ssh-keygen -t ed25519 -N '' -f /etc/it-suite/backup-offsite.key -C "rmm-backup@$(hostname)"
sudo cat /etc/it-suite/backup-offsite.key.pub
```

Then the destination, as `user@host:/path`:

```sh
read -rp 'rsync destination (user@host:/path): ' T && sudo sed -i -e 's|^OFFSITE=.*|OFFSITE=rsync|' -e "s|^OFFSITE_RSYNC_TARGET=.*|OFFSITE_RSYNC_TARGET=$T|" /etc/it-suite/backup.conf; unset T
```

And its host key, once: the copy runs with `StrictHostKeyChecking=yes`, so
an unknown host is refused. This connects as root, shows the destination's
fingerprint and asks: compare it with the one the destination's owner gives
you before you answer `yes`.

```sh
uh="$(sudo sed -n 's/^OFFSITE_RSYNC_TARGET=\([^:]*\):.*/\1/p' /etc/it-suite/backup.conf)" && sudo ssh -i /etc/it-suite/backup-offsite.key -o StrictHostKeyChecking=ask "$uh" true
```

**3b. Or to an S3 bucket** (any S3-compatible endpoint; a key that can only
write this bucket, and retention set on the bucket itself):

```sh
read -rp 'S3 endpoint (https://...): ' E && read -rp 'S3 bucket: ' B && read -rp 'S3 access key: ' A && sudo sed -i -e 's|^OFFSITE=.*|OFFSITE=s3|' -e "s|^OFFSITE_S3_ENDPOINT=.*|OFFSITE_S3_ENDPOINT=$E|" -e "s|^OFFSITE_S3_BUCKET=.*|OFFSITE_S3_BUCKET=$B|" -e "s|^OFFSITE_S3_ACCESS_KEY=.*|OFFSITE_S3_ACCESS_KEY=$A|" /etc/it-suite/backup.conf; unset E B A
```

The secret key at a prompt that does not echo it; it reaches `backup.conf`
on stdin, never in a command's arguments:

```sh
read -rsp 'S3 secret key: ' K && echo && printf 'OFFSITE_S3_SECRET_KEY=%s\n' "$K" | sudo sh -c 'grep -v "^OFFSITE_S3_SECRET_KEY=" /etc/it-suite/backup.conf > /etc/it-suite/backup.conf.new && cat >> /etc/it-suite/backup.conf.new && chmod 600 /etc/it-suite/backup.conf.new && mv /etc/it-suite/backup.conf.new /etc/it-suite/backup.conf'; unset K
```

**4. Check.** A backup now, and its off-host copy:

```sh
sudo systemctl start its-platform-backup.service
sudo cat /var/lib/it-suite/backup/last-offsite
```

The last line prints the time, the mode (`rsync` or `s3`) and the backup's
name. Platform status's *Off-host backup copy* check turns healthy, and from
now on alerts when a copy fails or is older than 26 hours.

## Rolling back a deployment

One ordered path. Read it before you need it.

```sh
cd /opt/it-suite/deploy

# 1. Decide what you are going back to, and whether the schema moved.
#    BACKUP is the backup the bad deploy took before it changed anything:
#    the newest pre-upgrade-* directory, the one deploy.sh named at its
#    end. Going back further, set BACKUP to that older directory instead,
#    and past own authentication phase 4 with the line in "Back past own
#    authentication phase 4" below.
BACKUP="$(ls -1d ~/backups/pre-upgrade-* | tail -1)"; echo "BACKUP=$BACKUP"
grep IT_SUITE_VERSION .env                        # the version running now
cat "$BACKUP/image_version"                       # the version to go BACK to
docker images --format '{{.Repository}}:{{.Tag}}' | grep itsuite/backend
docker compose exec -T postgres \
  psql -U its_admin -d itsuite -tAc 'select version_num from alembic_version;'
cat "$BACKUP/alembic_version"                     # what the backup holds
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
#    ITS_RESTORE_FORCE=1 IS REQUIRED HERE AND IT DROPS EVERY DATABASE THE BACKUP
#    HOLDS: `itsuite`, and the old identity provider's if it is in the backup.
#    restore.sh refuses a non-empty target by default; in a rollback the
#    target is ALWAYS non-empty, so that guard is for a mistyped restore, not
#    for this. Read what it costs, below, before running it.
ITS_RESTORE_FORCE=1 ./scripts/restore.sh "$BACKUP"

# 4. ONLY IF the schema moved: confirm the database is back where the old
#    images expect it.
docker compose exec -T postgres \
  psql -U its_admin -d itsuite -tAc 'select version_num from alembic_version;'

# 5. Point at the previous version and start. NO rebuild, no git checkout.
#    PREV is the image tag the backup recorded (its image_version), and only
#    a bare tag is taken: a backup from before versioned images recorded
#    none, and then the edit below refuses rather than write it. Edit the
#    line rather than appending: a `sed -i` on a .env that predates
#    IT_SUITE_VERSION silently matches nothing and leaves you on :dev.
PREV="$(sed -n 's/^IT_SUITE_VERSION=//p' "$BACKUP/image_version" | tail -1)"
case "$PREV" in ''|*[!A-Za-z0-9._-]*) echo "no image tag recorded in $BACKUP"; PREV="" ;; *) echo "PREV=$PREV" ;; esac
grep -q '^IT_SUITE_VERSION=' .env \
  && sed -i "s/^IT_SUITE_VERSION=.*/IT_SUITE_VERSION=${PREV:?no previous version}/" .env \
  || echo "IT_SUITE_VERSION=${PREV:?no previous version}" >> .env
grep IT_SUITE_VERSION .env                        # confirm exactly one line
docker compose up -d

# 6. Verify.
docker compose ps -a --format 'table {{.Service}}\t{{.Status}}'
curl -o /dev/null -w '%{http_code}\n' https://rmm.novieri.com/api/v1/devices   # 401
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

**Back past own authentication phase 4 (2026-11-15), the backup in step 3 is
the one taken before that upgrade: the newest `~/backups/pre-upgrade-*` whose
`alembic_version` is below `0248`.** In step 1, set `BACKUP` with the second
line here instead, which picks it by the rule `deploy.sh` uses (its
`way_back_backup`); the first lists every backup with its revision, and
the last line prints `none` when this host holds no backup from before
0248:

```sh
grep -H . ~/backups/pre-upgrade-*/alembic_version
BACKUP=""; for d in ~/backups/pre-upgrade-*/; do [ -r "$d/alembic_version" ] || continue; r="$(tr -d '[:space:]' < "$d/alembic_version")"; case "$r" in [0-9][0-9][0-9][0-9]) [ "$((10#$r))" -lt 248 ] && BACKUP="${d%/}" ;; esac; done
echo "BACKUP=${BACKUP:-none}"
```

A backup taken after it cannot cross its migration backwards, and holding
a dump of the old identity provider's database does not make one the right
one: every pre-upgrade backup taken while that database exists holds one. If
that upgrade stopped part-way and a second `deploy.sh` finished it, the right
one is the first run's: it holds no such dump, and needs none, because the
database stayed on the cluster and `restore.sh` touches only the databases a
backup holds; the second run names it, and its images, at its start and in
its closing rollback help. Both halves of the paragraph above apply, too:
check out the whole `deploy/` directory at the old release, not only
`docker-compose.yml` -- that release bind-mounts files this checkout no
longer has, and `deploy.sh` prints the exact `git checkout` at the end of the
upgrade -- and `.env` must still hold the four keys that release starts
with, which [Retiring the old identity provider's
data](#retiring-the-old-identity-providers-data) lists. Until you take that
step, they are there.

**Your rollback targets are local images, and `docker system prune` deletes
them.** With the tag gone, `docker compose up -d` rebuilds from the working
tree and tags the result with the old SHA — a rollback that silently
redeploys current source under a name that says otherwise. Keep at least the
last few `itsuite/backend` and `itsuite/web` tags, and prune by name rather
than wholesale.

## Retiring the old identity provider's data

**Only on a host that ran a release from before own authentication phase 4
(2026-11-15).** A fresh install never had any of this.

Phase 4 removed Keycloak from the stack, and `deploy.sh` removed its two
containers on the upgrade. What a host that ran it still keeps, on purpose:

- the `keycloak` database in the stack's Postgres -- the old realm, with its
  users' password hashes, its client secrets and the keys that signed its
  tokens;
- three files on the `its-secrets` volume: `keycloak-admin.secret`,
  `keycloak-events.secret` and `keycloak-bootstrap-admin.secret`;
- four `.env` keys the release before phase 4 needs to start -- its compose
  file does not parse without `KEYCLOAK_ADMIN_USER` and
  `KEYCLOAK_ADMIN_PASSWORD`, and its api, gateway and worker do not
  start without `ITS_OIDC_ISSUER` and `ITS_OIDC_JWKS_URL` -- and the other
  keys of the old identity provider, which nothing reads at all;
- the image `quay.io/keycloak/keycloak:26.2`.

Nothing in this release reads any of them. **The way back past phase 4 is the
backup taken before that upgrade** -- the newest `~/backups/pre-upgrade-*`
whose `alembic_version` is below `0248` -- restored as [Rolling back a
deployment](#rolling-back-a-deployment) says, with the four keys still in
`.env`, the database and the secret files still here, and `deploy/` checked
out at the old release. **Do not delete the keys, the secret files or the
database before the step below.**

**The step, at a time you choose, once you have checked the upgrade. It
closes the way back past phase 4:** after it, a release from before phase 4
no longer starts on this host (part 6 says what going back would then take). A
migration could have done it (Alembic can run a statement outside its
transaction), but it is irreversible, so it is your act and not `up -d`'s.
Every part can be run again, and a second run removes nothing more. In
`deploy/`:

1. **A final copy of the database, if you want one.** It goes to
   `~/backups/keycloak-final.dump`, beside the pre-upgrade backups and apart
   from the nightly ones, readable by you alone. A copy counts as good only
   if `pg_restore` reads it to the end (`-f /dev/null` reads every table's
   data; `-l` reads only the list at the front, which a dump cut off
   part-way still has). The second command dumps only when there is no
   good copy yet, into `keycloak-final.dump.part`, and renames it only once
   `pg_dump` has finished, so running this part again after part 2 cannot
   replace a good copy with a broken one:

   ```sh
   mkdir -p ~/backups
   docker compose exec -T postgres pg_restore -f /dev/null >/dev/null 2>&1 < ~/backups/keycloak-final.dump \
     || (umask 077 && docker compose exec -T postgres pg_dump -Fc -U its_admin keycloak > ~/backups/keycloak-final.dump.part \
         && mv ~/backups/keycloak-final.dump.part ~/backups/keycloak-final.dump)
   docker compose exec -T postgres pg_restore -f /dev/null < ~/backups/keycloak-final.dump && echo "dump ok"
   ```

   It prints `dump ok`. The copy holds password hashes and the realm's
   signing keys: keep it encrypted, like `globals.sql`, or skip this part.
2. **The database, and the image.** No container of the old identity
   provider may still be connected. `deploy.sh` removed them; the first two
   commands remove any left, found as `deploy.sh` finds them, by the labels
   Compose put on them (this project, and those two services), not by
   `compose ps`. The first prints this project's name (`it-suite` unless
   `.env` sets `COMPOSE_PROJECT_NAME`); the loop prints the id of each
   container it removed, and nothing when none is left:

   ```sh
   project="$(docker compose config --no-interpolate | awk '/^name: / { print $2; exit }')"; echo "project: $project"
   for service in keycloak keycloak-init; do
     docker ps -aq --filter "label=com.docker.compose.project=${project:?docker compose config named no project}" \
       --filter "label=com.docker.compose.service=$service"
   done | xargs -r docker rm -f
   docker compose exec -T postgres dropdb -U its_admin keycloak
   docker compose exec -T postgres psql -X -A -t -U its_admin -d postgres \
     -c "SELECT count(*) FROM pg_database WHERE datname = 'keycloak'"
   docker image rm quay.io/keycloak/keycloak:26.2
   ```

   The query prints `0`. On a second run `dropdb` answers that the database
   does not exist and `docker image rm` that there is no such image; the
   query still prints `0`.
3. **The three secret files**, from a one-off `api` container that Traefik
   is told to ignore (the `api` service mounts the volume, and runs as the
   user that wrote them):

   ```sh
   docker compose run --rm --no-deps --label traefik.enable=false --entrypoint rm api -f \
     /var/lib/its/secrets/keycloak-admin.secret \
     /var/lib/its/secrets/keycloak-events.secret \
     /var/lib/its/secrets/keycloak-bootstrap-admin.secret
   docker compose run --rm --no-deps --label traefik.enable=false --entrypoint ls api /var/lib/its/secrets
   ```

   The listing shows no `keycloak-` file.
4. **The four keys a rollback needed:**

   ```sh
   sed -i -E '/^(KEYCLOAK_ADMIN_USER|KEYCLOAK_ADMIN_PASSWORD|ITS_OIDC_ISSUER|ITS_OIDC_JWKS_URL)=/d' .env
   grep -cE '^(KEYCLOAK_ADMIN_USER|KEYCLOAK_ADMIN_PASSWORD|ITS_OIDC_ISSUER|ITS_OIDC_JWKS_URL)=' .env
   ```

   `grep -c` prints `0`.
5. **The other keys of the old identity provider**, the thirteen
   `deploy.sh` names besides those four, each by its whole name, so no other
   line of `.env` can match:

   ```sh
   sed -i -E '/^(ITS_OIDC_(AUDIENCE|ORG_CLAIM|CONSOLE_CLIENT_ID)|ITS_MULTI_TENANT|ENTRA_(TENANT_ID|CLIENT_ID|CLIENT_SECRET)|ITS_KEYCLOAK_(ADMIN_URL|REALM|EVENTS_CLIENT_ID|EVENTS_SECRET_PATH|ADMIN_CLIENT_ID|ADMIN_SECRET_PATH))=/d' .env
   grep -cE '^(KEYCLOAK_ADMIN_(USER|PASSWORD)|ITS_OIDC_(ISSUER|JWKS_URL|AUDIENCE|ORG_CLAIM|CONSOLE_CLIENT_ID)|ITS_MULTI_TENANT|ENTRA_(TENANT_ID|CLIENT_ID|CLIENT_SECRET)|ITS_KEYCLOAK_(ADMIN_URL|REALM|EVENTS_CLIENT_ID|EVENTS_SECRET_PATH|ADMIN_CLIENT_ID|ADMIN_SECRET_PATH))=' .env
   ```

   `grep -c` prints `0`, and the next `./scripts/deploy.sh` names no key.
   `ITS_DEFAULT_ORG_SLUG` stays. The containers that read `.env` see the
   change the next time they are recreated; nothing in them read those keys.
6. **This closed the way back.** From here, a release from before own
   authentication phase 4 does not start on this host: its compose file and
   its api refuse without the keys, and its identity provider has neither
   its database nor its secrets nor its image. Going back would mean
   restoring the backup from before the upgrade (`itsuite`; and `keycloak`
   only if that backup holds `keycloak.dump`, which the one a stopped
   upgrade took does not -- the final copy of part 1 does) and putting back
   by hand the four keys, the three secret files and the image. The final
   copy goes back in, after `restore.sh` and before the old release starts,
   with:

   ```sh
   docker compose exec -T postgres createdb -U its_admin keycloak
   docker compose exec -T postgres pg_restore -U its_admin -d keycloak < ~/backups/keycloak-final.dump
   ```

   `createdb` refuses if a `keycloak` database is already there: then the
   backup `restore.sh` used held one, and that is the one to keep.
7. **If a client secret was ever created in Entra for the old identity
   provider's Microsoft sign-in** (`ENTRA_CLIENT_SECRET`), revoke it there:
   in the Microsoft Entra admin center, **App registrations** → that
   application → **Certificates & secrets**. Deleting the line in part 5
   does not stop the secret working; nothing in this platform reads it any
   more.

Copies made before the step keep what it removed: every pre-upgrade backup
taken while the database existed, and the nightly and off-site backups
taken before the phase-4 upgrade, hold `keycloak.dump`, and a
hand-made copy of `its-secrets` holds the three files, until they age out
under your retention -- an off-site bucket with Object Lock or a lifecycle
rule may not let you delete them sooner. The next `./scripts/backup.sh`
writes no `keycloak.dump` and prints no note about the database.

⚠ **UNVERIFIED as a whole on a live host.** No host upgraded from before
phase 4 has run this step. CI's `deploy` job drops a `keycloak` database it
made and restores it from a pre-upgrade backup; the `.env` lines of parts 4
and 5 were run against the `.env.example` of the release before phase 4,
and the container query of part 2 against a simulated `docker`. Nothing
else here has run.

## Renewing the gateway certificate

365 days, no automatic rotation. Re-running the issuer *is* the renewal:

```sh
cd /opt/it-suite/deploy
sudo cat /etc/it-suite/gateway-issuer.token | docker compose run --rm --no-deps -T \
  -v "$PWD/scripts/issue-gateway-cert.py:/tmp/issue-gateway-cert.py:ro" \
  --entrypoint python gateway /tmp/issue-gateway-cert.py rmm.novieri.com
docker compose restart gateway
```

The token comes from the 0600 file on stdin, never from an argument — see step 6.
Drop the `-T` and the pipe to be prompted for it instead.

**Why this pipes and step 6b's timer redirects.** They are not
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
level=INFO msg="gateway connected" url=wss://rmm.novieri.com:8443/agent
```

so renewal is not an outage. The systemd timer is step 6b.

---

## What was broken, and how you would recognise it

The first-boot procedure had never been executed. Running it end to end found
eleven defects, all now fixed in this repository. A twelfth (#12) was found
later, by review rather than by running anything — it is the kind that a first
boot cannot surface, because it breaks on the *endpoint* rather than on this
host. #13 and #14 came from the first deploy that reached a **browser**: both
sit on the login path, both had been present since the beginning, and neither
is visible to any server-side test — which is why step 8 carried a
"no browser session, no login" marker for as long as it did. #15 to #17 came
from the first-boot rehearsal of 2026-11-24, which ran this runbook against
images built that day. They are listed because the symptoms are unhelpful and you will meet them
again if you pin different versions.

Rows 8, 9, 13 and 14 describe the identity provider this platform used until
own authentication phase 4 (2026-11-15); they are kept as the record of what
went wrong, and none of them can happen on a release after it.

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
| 9 | no way to federate AD/Entra or manage users | the Keycloak admin console (`/admin`) was not routed by Traefik at all | `/admin` and `/js` added to the router rule. **`/admin` has since been removed again** — a console that can reset every password in the fleet does not belong on the product's public hostname, and the answer to the original defect is Novieri RMM's own user management plus vendor break-glass over an SSH tunnel. `/js` stayed: sign-in needs it |
| 10 | first administrator authenticates but gets `403 requires ...` on everything | JIT user provisioning assigns **no role**, and nothing granted one | step 10 added to this runbook |
| 11 | `enroll: certificate and key do not form a pair: tls: failed to find any PEM data in certificate input` | OpenBao returns PEM **without a trailing newline**; the agent concatenates `cert_pem + chain_pem`, producing `-----END CERTIFICATE----------BEGIN CERTIFICATE-----`. **No device could enroll against the production CA backend.** Tests were green because they use `LocalCA`, whose PEM is newline-terminated | one-line normalisation in `backend/app/core/ca.py` (server-side, so it repairs agents already in the field) |
| 12 | an `app.deploy` job dispatches, reaches the endpoint, and fails at the artifact download with a name-resolution error; nothing in the control plane looks wrong | MinIO had **no published port and no Traefik router**, and presigned URLs were signed against `ITS_S3_ENDPOINT` (`http://minio:9000`) — a Docker-internal name. Every grant was correctly scoped, correctly time-bounded and unreachable | a `minio` router on `:443` matching `/<bucket>/`, and a separate `ITS_S3_PUBLIC_ENDPOINT` used only for signing (required in production). See "Object storage on :443" |
| 13 | the console's **Sign in** button does nothing; devtools shows repeated 0-byte `openid-configuration` fetches and no container logs anything | `lib/auth.ts` read the OIDC authority from `VITE_OIDC_AUTHORITY`, which is set **nowhere** in this repository, so every image ever built carried the fallback `http://localhost:8080/realms/it-suite` — each browser asked *its own machine* for the discovery document. Nothing server-side can log a request that never left the laptop | the authority is derived from `window.location.origin`; Keycloak is on the same origin under `/realms`, so it cannot drift from the deployment's hostname. `frontend/.env.development` keeps the override for the dev server. (Since own authentication phase 1 the console has no OIDC client at all -- `lib/auth.ts` is gone -- and fetches no discovery document) |
| 14 | Keycloak answers **`Invalid parameter: redirect_uri`** on a correctly-formed authorization request | the `it-suite-web` client shipped `redirectUris: ["https://*/auth/callback"]`. Keycloak honours a wildcard **only as a trailing `*`**; one in the host position is compared as a literal string and matches nothing. `rootUrl` was also `https://localhost`, so even a relative URI resolved to the wrong host | `rootUrl` emptied and the redirect made **relative** (`/auth/callback`), which resolves against the request and is therefore correct at any hostname. **A running realm is not re-imported** — an existing deployment must be patched through the admin API |
| 15 | `worker` and `backup-replicator` restart for ever, each log ending `ModuleNotFoundError: No module named 'packaging'` under `kombu/transport/redis.py`; the api, the gateway and jobs work, so nothing else looks wrong | the backend asked for `celery>=5.4` without its `redis` extra, although Redis is its broker. kombu 5.7.0's Redis transport imports `packaging`, which kombu declares only under that extra, so an image built from a fresh `pip install` had none. CI's `deploy` job did not start `worker` until 2026-11-24 (it now checks that it is healthy); `backup-replicator` is still never started in CI. The backend suite's environment has `packaging` through pytest | `celery[redis]>=5.4` in `backend/pyproject.toml` (`backend/tests/test_celery_broker_dependency.py`) |
| 16 | the first backup of a new host fails: `mc: <ERROR> Unable to stat source 'src/it-suite'. Bucket 'it-suite' does not exist.`, and `last-success` is never written | `backup.conf` mirrors the MinIO bucket (`OBJECTS=all`), which `ObjectStore` creates on first use; on the rehearsal nothing had used it, because #15 had stopped the replicator whose checks create it | `backup.sh` lists the buckets first: no bucket is nothing to mirror, and a listing that fails still fails the backup (`backend/tests/test_platform_backup_scripts.py`) |
| 17 | `build-images.sh` ends `Recent tags:` `(none yet)` and lists nothing under `built:`, with tags in the store | the docker CLI trims the spaces at both ends of a `--format` template, so the indented templates printed no indent and the greps for the indented form never matched | no indent in the templates; it is added after the match (`backend/tests/test_build_images_script.py`) |

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
the bucket name, so `https://rmm.novieri.com/it-suite/<key>` is already
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
  Per client: **5 GiB** total across the live versions of that client's
  documents, and 5 GiB more for the documents for every client, checked twice
  per upload — once before the body is read, once after (per client since
  2026-10-29). The quota exists because **this bucket is shared with backup
  repositories**: without it, a technician-tier `docs.manage` holder filling
  the volume stops backups from writing. Being per client, the documents'
  share of the volume grows with the number of clients — up to 5 GiB times
  (clients + 1) — so size the volume for that. Both are module constants (`app/modules/documentation/documents.py`)
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
  `docker compose up migrate` on an existing deployment, run this from
  `deploy/`. psql reads the password from the postgres container's own
  environment (`\getenv`; compose gives it `.env`'s `PORTAL_DB_PASSWORD`),
  so it is never an argument of any process:
  ```sh
  docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U its_admin -d itsuite <<'SQL'
  \getenv pw PORTAL_DB_PASSWORD
  ALTER ROLE portal_user LOGIN PASSWORD :'pw';
  SQL
  ```
  It must be the exact same password as `.env`'s `PORTAL_DB_PASSWORD` —
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

From `deploy/`, this asks for the two values only you know, the model
server's address and the model's name, deletes any earlier line of the
five settings, and writes four of them to `deploy/.env` (the `api` service
reads it via `env_file`; `.env.example` carries the same block commented
out). The fifth, the key's path, is written with the key below, so it is
set exactly when the model server takes one:

```sh
# ITS_AI_PROVIDERS names ONE provider key; two is an error.
# ITS_AI_LOCAL_KIND is "openai_compatible" or "anthropic".
read -rp 'Model server address (for example http://vllm.internal:8000/v1): ' AI_URL \
  && read -rp 'Model name, as that server names it: ' AI_MODEL \
  && [ -n "$AI_URL" ] && [ -n "$AI_MODEL" ] \
  && sed -i -E '/^ITS_AI_(PROVIDERS|LOCAL_KIND|LOCAL_BASE_URL|LOCAL_MODEL|LOCAL_API_KEY_PATH)=/d' .env \
  && printf '%s\n' 'ITS_AI_PROVIDERS=local' 'ITS_AI_LOCAL_KIND=openai_compatible' \
       "ITS_AI_LOCAL_BASE_URL=$AI_URL" "ITS_AI_LOCAL_MODEL=$AI_MODEL" >> .env
```

```sh
grep '^ITS_AI_' .env
```

**Restart the `api` service after editing them** — the environment is read
once, at startup.

**The key path must be on the `its-secrets` volume**, which the `api` service
mounts at `/var/lib/its/secrets` — the same convention `ITS_AUDIT_HMAC_KEY_PATH`
already uses. This is not cosmetic. Nothing in the stack mounts `/run/secrets`,
so a path there is a `FileNotFoundError` at the first hosted-provider call; and
`its-secrets` is the volume `docs/07-backup-restore.md` §2.2 tars, so a key
written anywhere else is key material outside your backup set. Create it the way
you created the audit key. The prompt waits for your paste:

```sh
read -rsp 'Provider API key: ' K && echo && \
printf '%s' "${K:?nothing was pasted}" | docker compose run --rm --no-deps -T --label traefik.enable=false \
  --entrypoint sh api -c 'umask 077 && cat > /var/lib/its/secrets/ai-provider.key && \
   chmod 600 /var/lib/its/secrets/ai-provider.key'; unset K
```

Then point the setting at it (the `grep` keeps a second run from adding
the line twice):

```sh
grep -qx 'ITS_AI_LOCAL_API_KEY_PATH=/var/lib/its/secrets/ai-provider.key' .env \
  || echo 'ITS_AI_LOCAL_API_KEY_PATH=/var/lib/its/secrets/ai-provider.key' >> .env
```

The `read` does not echo the key, and `printf` is a shell builtin, so the
key reaches the container on its standard input and is never an argument
of any process. Restart the `api` service for the new setting.

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

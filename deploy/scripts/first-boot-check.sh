#!/usr/bin/env bash
#
# Prove that the first boot worked: deploy/README.md, First boot, 3 -- steps
# 10, 11a, 11c to 11f and 12 of its appendix (design
# 2026-11-27-despliegue-automatizado §2, §3).
#
#   ./scripts/first-boot-check.sh
#
# Run it after first-boot.sh and the browser: the installation claimed (step
# 8), your second factor enrolled (step 9), an enrollment token in hand (step
# 11b). It asks for that token (hidden), starts a throwaway agent on this VPS
# (11c), says when to press Run job -> Collect inventory and waits for the job
# (11d), verifies the audit chain (11e), retires the agent (11f) and takes the
# first backup (12). It prints `run` before what it changes, `skip` for what
# it finds done, `ok` for a check that held. It is safe to run again, after a
# failure or after a success: a run that stops, for any reason (a failed check,
# Ctrl-C, a dropped SSH session), retires its agent, with its token and key, on
# the way out, and says how to go on; a re-run enrols, and retires, another
# throwaway agent, and the earlier one stays in the console as a record,
# offline. Only a run killed outright (SIGKILL, the OOM killer, a power cut), or a
# Docker that will not remove the container (the script says so, with the
# command), can leave its agent up; the next run then finds it and goes on from
# 11d. Its first two lines say so.
#
# The token reaches the agent in a 0600 file in its data directory, as the
# runbook puts it there, and nowhere else: never an argument, never printed.
#
# Options: --tls-cafile PEM (the root :443 chains to, for the curls and the
# agent: Let's Encrypt staging's on a rehearsal, Pebble's in CI); --connect
# ADDRESS (dial ITS_PUBLIC_HOST at this address instead of asking DNS: the
# CI); --wait-job SECONDS (default 600, step 11d). Environment, for the CI and
# a rehearsal: ITS_NO_SYSTEMCTL=1 (step 12 runs the backup service's command
# itself) and ITS_SYSTEMD_DIR; for its tests: ITS_SMOKE_AGENT_DIR (the agent's
# data directory, /tmp/smoke-agent) and ITS_BACKUP_STATE_DIR
# (scheduled-backup.sh's own).
set -euo pipefail

case "$-" in
  *x*) printf 'first-boot-check.sh: refusing to run under xtrace (bash -x): it would print the token\n' >&2
       exit 1 ;;
esac

if [ -t 1 ]; then bold=$'\033[1m'; plain=$'\033[0m'; else bold=""; plain=""; fi
step="starting"
say()  { step="$*"; printf '\n%s%s%s\n' "$bold" "$*" "$plain"; }
ok()   { printf '  ok      %s\n' "$*"; }
skip() { printf '  skip    %s\n' "$*"; }
act()  { printf '  run     %s\n' "$*"; }
note() { printf '  note    %s\n' "$*"; }

# From the moment the throwaway agent is started (agent_live=1), every way out
# but success retires it: the container, and its data directory with the token
# and the key. A die, a command that failed outside die (the EXIT trap), Ctrl-C,
# a TERM, a HUP (a dropped SSH session) and a PIPE (nobody reads its output
# any more) all land here. The cleanup ignores
# further signals, so a second Ctrl-C cannot cut it short, and clears
# agent_live only once it has tried everything.
agent_live=0 failed=0 retire_msg="" rerun_ok=1
retire_agent() {
  [ "$agent_live" = 1 ] || return 0
  trap '' INT TERM HUP PIPE
  local bad=0
  # The agent's own account of itself goes before the container does.
  docker logs --tail 20 smoke-agent 2>&1 </dev/null | sed 's/^/        agent: /' >&2 || true
  docker rm -f smoke-agent >/dev/null 2>&1 </dev/null || bad=1
  rm -rf "$agent_data" || bad=1
  agent_live=0
  if [ "$bad" = 0 ]; then
    retire_msg="The throwaway agent was retired: smoke-agent is removed, and so are $agent_data, its token and key. Its device stays in the console, offline."
    [ "$rerun_ok" = 0 ] || retire_msg="$retire_msg A re-run enrols a new one: have a new enrollment token ready (step 11b)."
  else
    retire_msg="The throwaway agent could NOT be retired. Do it now: docker rm -f smoke-agent && rm -rf $agent_data (its device stays in the console, offline)."
  fi
}
die() {
  local msg="$1" line
  shift
  trap '' INT TERM HUP PIPE
  failed=1
  # Said first: nothing the cleanup meets can lose it.
  # A write that fails (a hung-up terminal, a pipe with no reader) must never
  # keep the cleanup from running: every one of these is allowed to fail.
  printf '\nFAILED  %s\n' "$msg" >&2 || true
  for line in "$@"; do printf '        %s\n' "$line" >&2 || true; done
  retire_agent
  [ -z "$retire_msg" ] || printf '        %s\n' "$retire_msg" >&2 || true
  exit 1
}
on_exit() {
  local rc=$?
  trap '' INT TERM HUP PIPE
  trap - EXIT
  [ "$rc" != 0 ] || return 0
  [ "$failed" = 0 ] || return 0   # die has said it
  failed=1
  case "$rc" in
    129|130|143) printf '\nFAILED  %s: interrupted (exit %s)\n' "$step" "$rc" >&2 || true ;;
    141) printf '\nFAILED  %s: interrupted (its output was closed) (exit %s)\n' "$step" "$rc" >&2 || true ;;
    *) printf '\nFAILED  %s: a command failed (exit %s); what it printed is above\n' "$step" "$rc" >&2 || true ;;
  esac
  printf '        %s\n' "It is safe to run again: ./scripts/first-boot-check.sh" >&2 || true
  retire_agent
  [ -z "$retire_msg" ] || printf '        %s\n' "$retire_msg" >&2 || true
  exit 1
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
trap 'exit 141' PIPE

tls_cafile="" connect="" wait_job=600
while [ $# -gt 0 ]; do
  case "$1" in
    --tls-cafile) [ $# -ge 2 ] || die "--tls-cafile needs a file" "./scripts/first-boot-check.sh --help"; tls_cafile="$2"; shift 2 ;;
    --connect) [ $# -ge 2 ] || die "--connect needs an address" "./scripts/first-boot-check.sh --help"; connect="$2"; shift 2 ;;
    --wait-job) [ $# -ge 2 ] || die "--wait-job needs seconds" "./scripts/first-boot-check.sh --help"; wait_job="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" "./scripts/first-boot-check.sh --help" ;;
  esac
done
[[ $wait_job =~ ^[1-9][0-9]*$ ]] || die "--wait-job needs a whole number of seconds" "./scripts/first-boot-check.sh --help"
if [ -n "$connect" ]; then
  [[ $connect =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "--connect needs an IPv4 address, such as 127.0.0.1" "./scripts/first-boot-check.sh --help"
fi
# Resolved before the cd below, and absolute because docker mounts it.
if [ -n "$tls_cafile" ]; then
  [ -s "$tls_cafile" ] || die "--tls-cafile $tls_cafile is missing or empty" "./scripts/first-boot-check.sh --help"
  tls_cafile="$(readlink -f "$tls_cafile")"
fi

cd "$(dirname "$(readlink -f "$0")")/.."   # deploy/
note "this enrols a throwaway agent on this VPS (step 11c) and retires it, with its key (11f)."
note "It is safe to run again: a second run enrols and retires another; the first stays in the console, offline."
unit_dir="${ITS_SYSTEMD_DIR:-/etc/systemd/system}"
no_systemctl="${ITS_NO_SYSTEMCTL:-0}"
backup_state="${ITS_BACKUP_STATE_DIR:-/var/lib/it-suite/backup}"   # scheduled-backup.sh's
agent_data="${ITS_SMOKE_AGENT_DIR:-/tmp/smoke-agent}"
agent_bin="$PWD/../agent/dist/novieri-agent"
[ -f .env ] || die ".env is missing: this runs after first-boot.sh" "./scripts/first-boot.sh"
host="$(sed -n 's/^ITS_PUBLIC_HOST=//p' .env | tail -1)"
[ -n "$host" ] || die ".env has no ITS_PUBLIC_HOST" "./scripts/first-boot.sh"

dc() { docker compose "$@" </dev/null; }
psql_admin() { dc exec -T postgres psql -X -U its_admin -d itsuite -tAc "$1"; }

# ------------------------------------------------------------ OpenBao

say "OpenBao"
rc=0
dc exec -T openbao bao status >/dev/null 2>&1 || rc=$?
case "$rc" in
  0) ok "OpenBao answers, unsealed (bao status: Sealed false)" ;;
  2) die "OpenBao is sealed" "deploy/README.md, After a reboot or a restart: sudo ./scripts/openbao-seal.sh status" "then: ./scripts/first-boot-check.sh" ;;
  *) die "OpenBao does not answer" "docker compose up -d && docker compose exec openbao bao status" "then: ./scripts/first-boot-check.sh" ;;
esac

# ------------------------------------------------------------ step 10

say "Step 10 — an administrator"
admins="$(psql_admin "select u.email, r.name from users u join user_roles ur on ur.user_id = u.id join roles r on r.id = ur.role_id where u.kind = 'staff'" | tr -d '\r')" \
  || die "step 10: the database did not answer" "docker compose ps postgres && docker compose logs --tail 50 postgres" "then: ./scripts/first-boot-check.sh"
printf '%s\n' "$admins" | sed '/^$/d; s/^/        /'
case "$admins" in
  *"|org-admin"*) ok "step 10: a staff account holds org-admin" ;;
  *) die "no staff account holds org-admin: the installation is not claimed, or the claim did not complete" \
         "in a browser: https://$host/ with the install code (docker compose exec api cat /var/lib/its/secrets/setup-code)," \
         "then sign in and enrol your second factor (deploy/README.md, First boot, 2); then: ./scripts/first-boot-check.sh" ;;
esac

# ----------------------------------------------------------- step 11a

say "Step 11a — authorization is enforced server-side"
curl_args=(-s -o /dev/null -w '%{http_code}')
[ -z "$tls_cafile" ] || curl_args+=(--cacert "$tls_cafile")
[ -z "$connect" ] || curl_args+=(--noproxy '*' --resolve "$host:443:$connect")
refused=""
for path in /api/v1/devices /api/v1/jobs /api/v1/audit/verify /api/v1/enrollment-tokens; do
  code="$(curl "${curl_args[@]}" "https://$host$path" </dev/null || true)"
  printf '  %-28s %s\n' "$path" "$code"
  [ "$code" = 401 ] || refused="$refused $path=$code"
done
[ -z "$refused" ] || die "unauthenticated, these did not answer 401:$refused" \
  "a 404 on all four means Traefik is not routing (deploy/README.md, What was broken, defect 7): docker compose logs --tail 50 traefik"
ok "step 11a: all four answer 401 to a request with no session"

# ------------------------------------------------------ steps 11b, 11c

read_token() {
  token=""
  IFS= read -r -s -p "Enrollment token from step 11b (hidden): " token || [ -n "$token" ] \
    || die "the input ended before the enrollment token" "./scripts/first-boot-check.sh, and paste it at the prompt"
  if [ -t 0 ]; then echo; fi
  token="${token//[[:space:]]/}"
  [ -n "$token" ] || die "the enrollment token was empty" "./scripts/first-boot-check.sh, and paste it at the prompt"
}

say "Step 11c — a throwaway agent on this VPS"
if [ -n "$(docker ps -q --filter name='^smoke-agent$' --filter status=running </dev/null)" ]; then
  skip "steps 11b and 11c: the throwaway agent smoke-agent is running, from an earlier run"
  agent_live=1
  rm -f "$agent_data/enroll-token"
else
  docker rm -f smoke-agent >/dev/null 2>&1 </dev/null || true
  rm -rf "$agent_data"   # a token left by an earlier failed run goes too
  if [ -x "$agent_bin" ] && [ -z "$(find ../agent -path ../agent/dist -prune -o -type f -newer "$agent_bin" -print -quit)" ]; then
    skip "step 11c: agent/dist/novieri-agent is newer than every file of agent/"
  else
    act "go build, in golang:1.24-bookworm, as $(id -un)"
    docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/../agent:/src" -w /src -e CGO_ENABLED=0 \
      -e GOCACHE=/tmp/go-cache -e GOMODCACHE=/tmp/go-mod golang:1.24-bookworm \
      go build -trimpath -o dist/novieri-agent ./cmd/agent </dev/null
  fi
  note "step 11b, in the console: https://$host/enrollment -- a token named first endpoint, 24 hours, one use,"
  note "for the site Principal of your own organization's client. It is shown once: paste it here."
  read_token
  agent_live=1   # from here the token is on disk: every way out retires it
  install -d -m 700 "$agent_data"
  (umask 077 && printf '%s' "$token" > "$agent_data/enroll-token")
  token=""
  act "the agent, in busybox:1.36.1 on this host's network: it dials https://$host"
  run_args=(-d --name smoke-agent --network host --user "$(id -u):$(id -g)"
    -v "$agent_bin:/usr/local/bin/novieri-agent:ro"
    -v "${tls_cafile:-/etc/ssl/certs/ca-certificates.crt}:/etc/ssl/certs/ca-certificates.crt:ro"
    -v "$agent_data:/var/lib/novieri-agent"
    -e "ITS_AGENT_ENROLL_URL=https://$host" -e ITS_AGENT_DATA_DIR=/var/lib/novieri-agent)
  [ -z "$connect" ] || run_args+=(--add-host "$host:$connect")
  docker run "${run_args[@]}" busybox:1.36.1 /usr/local/bin/novieri-agent </dev/null >/dev/null
fi
for _ in $(seq 1 30); do
  docker logs smoke-agent 2>&1 </dev/null | grep -c 'gateway connected' >/dev/null && break
  sleep 2
done
logs="$(docker logs smoke-agent 2>&1 </dev/null || true)"
printf '%s\n' "$logs" | grep -E 'starting|enrolling|enrolled|gateway connected' | sed 's/^/        /' || true
case "$logs" in
  *"gateway connected"*) ;;
  *) die "the agent did not connect to the gateway within 60 s" "the agent's last log lines are above" \
         "a token that was used, or is past its 24 hours, is refused: make a new one in the console (step 11b), then: ./scripts/first-boot-check.sh" ;;
esac
device_id="$(printf '%s\n' "$logs" | sed -n 's/.*msg=enrolled device_id=\([0-9a-f-]*\).*/\1/p' | tail -1)"
[[ $device_id =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
  || die "the agent's log names no device it enrolled as" "the agent's last log lines are above" "make a new enrollment token (step 11b), then: ./scripts/first-boot-check.sh"
[ ! -e "$agent_data/enroll-token" ] || die "the agent kept its enrollment token after enrolling" "the agent's last log lines are above" "make a new enrollment token (step 11b), then: ./scripts/first-boot-check.sh"
ok "step 11c: the agent enrolled as device $device_id, connected over :8443, and its token file is gone"

# ----------------------------------------------------------- step 11d

say "Step 11d — round-trip a signed job"
note "NOW, in the console: Devices -> $(hostname), the one that is online (device id $device_id) -> Run job -> Collect inventory."
note "Waiting up to $wait_job s for a job a person asked for to reach this device and come back."
deadline=$(( $(date +%s) + wait_job ))
# Only jobs issued after this agent enrolled: a refused row of an earlier run's
# agent is not this agent's. issued_at is cut to whole seconds, so is the anchor.
query="select id, type, status, signature is not null from jobs where target_type = 'device' and target_id = '$device_id' and issued_at >= date_trunc('second', (select created_at from agents where device_id = '$device_id')) and requested_by is not null order by issued_at desc limit 1"
while :; do
  row="$(psql_admin "$query" | tr -d '\r')" || die "step 11d: the database did not answer" "docker compose logs --tail 50 postgres" "then: ./scripts/first-boot-check.sh"
  case "$row" in
    *"|succeeded|t") break ;;
    *"|succeeded|f")
      rerun_ok=0
      die "INVARIANT 1 BROKEN: a job succeeded on the agent with no server signature (id|type|status|signed: $row)" \
          "No unsigned command path may exist (CLAUDE.md, security invariant 1). This is a security failure, not a flake." \
          "Do not put this host in service, and do not run this check again." \
          "Keep the audit log and the api logs as they are, and raise it: docker compose logs --tail 100 api" \
          "The agent's last log lines follow, taken before it is retired." ;;
    *"|failed|"*|*"|expired|"*|*"|refused|"*|*"|cancelled|"*)
      die "the job ended: $row" \
          "Run it again with a new agent: make a new enrollment token (step 11b), request Run job -> Collect inventory when asked, then from deploy/: ./scripts/first-boot-check.sh" \
          "(add the same --tls-cafile and --connect options if you used them)" ;;
  esac
  [ "$(date +%s)" -lt "$deadline" ] || die "no job a person asked for reached this device in $wait_job s" \
    "make a new enrollment token (step 11b), then from deploy/: ./scripts/first-boot-check.sh, and press Run job -> Collect inventory on the device it names"
  sleep 3
done
printf '        %s\n' "$row"
ok "step 11d: signed by the server, verified by the agent before it ran, and its result back over :8443"

# ----------------------------------------------------------- step 11e

say "Step 11e — the audit chain"
# Not through dc(): its </dev/null would take the place of this here-document.
chain="$(docker compose exec -T api python - <<'EOF'
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
)" || die "the audit chain could not be read" "docker compose logs --tail 50 api" "then: ./scripts/first-boot-check.sh"
chain="$(printf '%s\n' "$chain" | tr -d '\r')"
printf '%s\n' "$chain" | sed 's/^/        /'
case "$chain" in
  *'"ok": true'*) ;;
  *) die "no organization's audit chain verifies" "docker compose logs --tail 50 api" "then: ./scripts/first-boot-check.sh" ;;
esac
case "$chain" in
  *'"ok": false'*) die "an audit chain is broken (above): restoring the database without its-secrets does this" "docs/07-backup-restore.md §4" "then: ./scripts/first-boot-check.sh" ;;
esac
ok "step 11e: every organization's audit chain verifies"

# ----------------------------------------------------------- step 11f

say "Step 11f — retire the throwaway agent"
act "docker rm -f smoke-agent; rm -rf $agent_data"
docker rm -f smoke-agent >/dev/null </dev/null \
  || die "step 11f: could not remove smoke-agent" "docker rm -f smoke-agent && rm -rf $agent_data" "then: ./scripts/first-boot-check.sh"
rm -rf "$agent_data" || die "step 11f: could not remove $agent_data" "rm -rf $agent_data" "then: ./scripts/first-boot-check.sh"
agent_live=0
ok "step 11f: the agent and its key are gone; the device stays in the console as a record, offline"

# ------------------------------------------------------------ step 12

backup_timer_installed() {
  if [ "$no_systemctl" = 1 ]; then [ -e "$unit_dir/its-platform-backup.timer" ]
  else [ "$(systemctl is-enabled its-platform-backup.timer 2>/dev/null || true)" = enabled ]; fi
}

say "Step 12 — the first backup, so that there is a way back"
last="$(sudo cat "$backup_state/last-success" 2>/dev/null || true)"
if [ -n "$last" ] && backup_timer_installed; then
  skip "step 12: the nightly backup timer is installed, and a backup exists"
else
  act "install-backup-timer.sh, then a backup now"
  if [ "$no_systemctl" = 1 ]; then
    sudo env ITS_NO_SYSTEMCTL=1 ITS_SYSTEMD_DIR="$unit_dir" ./scripts/install-backup-timer.sh </dev/null \
      || die "step 12: install-backup-timer.sh failed (its output is above)" "sudo ./scripts/install-backup-timer.sh" "then: ./scripts/first-boot-check.sh"
    note "ITS_NO_SYSTEMCTL=1: the backup service's command, run here"
    sudo ./scripts/scheduled-backup.sh </dev/null \
      || die "step 12: the first backup failed (its output is above)" "sudo ./scripts/scheduled-backup.sh" "then: ./scripts/first-boot-check.sh"
  else
    sudo ./scripts/install-backup-timer.sh </dev/null \
      || die "step 12: install-backup-timer.sh failed (its output is above)" "sudo ./scripts/install-backup-timer.sh" "then: ./scripts/first-boot-check.sh"
    if ! sudo systemctl start its-platform-backup.service; then
      sudo journalctl -u its-platform-backup.service --no-pager -n 50 || true
      die "step 12: the backup service failed (its journal is above)" "sudo systemctl status its-platform-backup.service" "then: ./scripts/first-boot-check.sh"
    fi
    sudo journalctl -u its-platform-backup.service --no-pager -n 20 || true
  fi
  last="$(sudo cat "$backup_state/last-success" 2>/dev/null || true)"
  [ -n "$last" ] || die "the first backup did not succeed: no last-success was written" \
    "sudo journalctl -u its-platform-backup.service --no-pager -n 50 (on a host without systemd: sudo ./scripts/scheduled-backup.sh)" "then: ./scripts/first-boot-check.sh"
fi
ok "step 12: the newest backup: $last"
note "That backup is on this VPS and goes with it. The off-host copy is recommended before anything"
note "real is enrolled: deploy/README.md, The off-host copy, step by step."

say "First boot is complete."
note "Custody, off this VPS, now (deploy/README.md, First boot, 4): the five shares and the root token"
note "(recovery keys), the unseal key file, the ten recovery codes, .env and the its-secrets, its-ca"
note "and openbao-data volumes -- GO-LIVE.md §6."

#!/usr/bin/env bash
#
# The first boot of a new Novieri RMM host, as deploy/README.md's appendix
# numbers it: steps 1 to 3.6; OpenBao's auto-unseal and the restart that
# proves it; steps 4 to 7; and step 8's install code (design
# 2026-11-27-despliegue-automatizado §2 to §4).
#
#   ./scripts/first-boot.sh                    # rmm.novieri.com, from deploy/
#   ./scripts/first-boot.sh --acme-ca-server https://acme-staging-v02.api.letsencrypt.org/directory \
#       --tls-cafile /tmp/le-staging-roots.pem # a rehearsal against staging
#
# WHERE IT STOPS FOR YOU. The Let's Encrypt contact address (new-env.sh asks
# it); the five OpenBao shares and the root token, shown ONCE, until you type
# SAVED; three of those shares, hidden, which unseal OpenBao and prove your
# copies; and your password, when sudo asks for it.
#
# RUN IT AGAIN after any failure. Each step checks first whether it is done
# and prints `skip` when it is; `run` when it changes something; `ok` for a
# check that held. It never initialises an OpenBao twice and never replaces
# .env, the audit key or a token file. Run again after `bao operator init`,
# it asks for what it no longer holds in memory: the root token (hidden) if
# .env does not hold the tokens yet, and three shares if OpenBao is sealed and
# the auto-unseal is not on yet. From step 4 on OpenBao unseals itself, so a
# failure there is run again with no share at all.
#
# IT REFUSES a mixed state -- .env and OpenBao (or the volumes) from two
# different first boots -- and says how to start from zero. And it refuses to
# print the shares anywhere but a terminal: with its output redirected it
# stops before `bao operator init`, unless ITS_FIRST_BOOT_ALLOW_NONTTY=1 (the
# CI's `deploy` job, whose log goes with its runner).
#
# SECRETS. None goes on a command line, into a log, a temporary file or the
# shell history: each reaches `bao` and `sudo tee` on stdin. They are written
# where the runbook writes them: .env (0600), the gateway-issuer token file
# (0600, root) and openbao-seal.sh's key file (0400). It refuses to run under
# `bash -x`.
#
# Options: --host NAME (a new .env only; default rmm.novieri.com),
# --acme-email ADDRESS and --acme-ca-server URL (passed to new-env.sh),
# --tls-cafile PEM (passed to check-public-tls.sh: Let's Encrypt staging's
# roots on a rehearsal, Pebble's in CI). Environment, for the CI and a
# rehearsal: ITS_FIRST_BOOT_ALLOW_NONTTY=1, ITS_NO_SYSTEMCTL=1 (step 6b writes
# its units and touches no systemd), ITS_SYSTEMD_DIR, ITS_ETC_DIR.
set -euo pipefail

case "$-" in
  *x*) printf 'first-boot.sh: refusing to run under xtrace (bash -x): it would print every secret\n' >&2
       exit 1 ;;
esac

if [ -t 1 ]; then bold=$'\033[1m'; plain=$'\033[0m'; else bold=""; plain=""; fi
say()  { printf '\n%s%s%s\n' "$bold" "$*" "$plain"; }
ok()   { printf '  ok      %s\n' "$*"; }
skip() { printf '  skip    %s\n' "$*"; }
act()  { printf '  run     %s\n' "$*"; }
note() { printf '  note    %s\n' "$*"; }
warn() { printf '  WARNING %s\n' "$*" >&2; }
# die WHAT [NEXT...]: what failed, then the commands to go on with.
die() {
  printf '\nFAILED  %s\n' "$1" >&2
  shift
  local line
  for line in "$@"; do printf '        %s\n' "$line" >&2; done
  exit 1
}

host="" email="" ca_server="" tls_cafile=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host) [ $# -ge 2 ] || die "--host needs a value"; host="$2"; shift 2 ;;
    --acme-email) [ $# -ge 2 ] || die "--acme-email needs a value"; email="$2"; shift 2 ;;
    --acme-ca-server) [ $# -ge 2 ] || die "--acme-ca-server needs a value"; ca_server="$2"; shift 2 ;;
    --tls-cafile) [ $# -ge 2 ] || die "--tls-cafile needs a file"; tls_cafile="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" "./scripts/first-boot.sh --help" ;;
  esac
done
# Resolved before the cd below: a relative path names the caller's directory.
if [ -n "$tls_cafile" ]; then
  [ -s "$tls_cafile" ] || die "--tls-cafile $tls_cafile is missing or empty"
  tls_cafile="$(readlink -f "$tls_cafile")"
fi

cd "$(dirname "$(readlink -f "$0")")/.."   # deploy/
etc_dir="${ITS_ETC_DIR:-/etc/it-suite}"
allow_nontty="${ITS_FIRST_BOOT_ALLOW_NONTTY:-0}"
token_file="$etc_dir/gateway-issuer.token"
key_file="$etc_dir/openbao-unseal/unseal.key"
command -v docker >/dev/null 2>&1 || die "docker is not on PATH" "sudo ./scripts/prepare-host.sh, then log out and in again"

# ------------------------------------------------------------- helpers

# Every compose call that must not read this script's stdin: what the
# operator types, or the CI pipes, is for the prompts.
dc() { docker compose "$@" </dev/null; }

env_get() { sed -n "s/^$1=//p" .env 2>/dev/null | tail -1; }
# The key's earlier line goes first: a second KEY= further down silently wins.
env_put() { sed -i "/^$1=/d" .env && printf '%s=%s\n' "$1" "$2" >> .env; }
env_token_set() { local v; v="$(env_get "$1")"; [ -n "$v" ] && [ "$v" != change-me ]; }
auto_unseal_on() { grep -qE '^COMPOSE_FILE=.*openbao/auto-unseal\.yml' .env 2>/dev/null; }

# A COMPOSE_FILE inherited from the environment (the CI's Pebble overlay, a
# rehearsal's) wins over the one openbao-seal.sh writes into .env: once .env
# has the auto-unseal overlay, it is added to the inherited one too, or the
# next `docker compose up` would recreate OpenBao without the seal its data
# is under.
inherited_compose_file="${COMPOSE_FILE:-}"
follow_auto_unseal() {
  [ -n "$inherited_compose_file" ] || return 0
  auto_unseal_on || return 0
  case ":$COMPOSE_FILE:" in *":openbao/auto-unseal.yml:"*) return 0 ;; esac
  export COMPOSE_FILE="$COMPOSE_FILE:openbao/auto-unseal.yml"
  note "COMPOSE_FILE comes from the environment and wins over .env's: openbao/auto-unseal.yml is added to it for this run"
}

# bao_state sets bao_up (yes|no), bao_init, bao_sealed (true|false) and
# bao_type. `bao status` exits 0 unsealed and 2 sealed, initialised or not, and
# prints its JSON either way; anything else is "not answering". (`bao operator
# init -status` is no help: it exits 2 for "not initialised" AND for a refused
# connection, measured with OpenBao 2.6.1 on 2026-11-27.)
bao_state() {
  local out rc=0
  out="$(dc exec -T openbao bao status -format=json 2>/dev/null)" || rc=$?
  bao_up=no bao_init="" bao_sealed="" bao_type=""
  case "$rc" in 0|2) ;; *) return 0 ;; esac
  bao_init="$(printf '%s\n' "$out" | sed -n 's/^ *"initialized": *\(true\|false\).*/\1/p' | head -1)"
  bao_sealed="$(printf '%s\n' "$out" | sed -n 's/^ *"sealed": *\(true\|false\).*/\1/p' | head -1)"
  bao_type="$(printf '%s\n' "$out" | sed -n 's/^ *"type": *"\([a-z-]*\)".*/\1/p' | head -1)"
  if [ -n "$bao_init" ] && [ -n "$bao_sealed" ]; then bao_up=yes; fi
  return 0
}

wait_bao_up() {
  local i
  for ((i = 0; i < 90; i += 3)); do
    bao_state
    [ "$bao_up" = yes ] && return 0
    sleep 3
  done
  die "OpenBao does not answer after 90 s" "docker compose logs --tail 50 openbao"
}

# wait_unsealed: up to 90 s for OpenBao to report itself unsealed.
wait_unsealed() {
  local i
  for ((i = 0; i < 90; i += 3)); do
    bao_state
    [ "$bao_sealed" = false ] && return 0
    sleep 3
  done
  return 1
}

# bao_with TOKEN COMMAND...: one command in the openbao container, run with
# TOKEN, which reaches it on stdin and its environment there, never an argv.
bao_with() {
  local token="$1"
  shift
  # shellcheck disable=SC2016  # expanded by the container's shell
  printf '%s\n' "$token" \
    | docker compose exec -T openbao sh -c 'read -r BAO_TOKEN && export BAO_TOKEN && exec "$@"' sh "$@"
}

# No `| grep -q` under pipefail: grep leaving early is a SIGPIPE upstream, and a false "no".
is_root_token() {
  local out
  out="$(bao_with "$1" bao token lookup -format=json 2>/dev/null)" || return 1
  out="${out//[[:space:]]/}"
  [[ $out == *'"policies":["root"]'* ]]
}

is_one_of() {
  local want="$1" have
  shift
  for have in "$@"; do [ "$have" = "$want" ] && return 0; done
  return 1
}

psql_admin() { dc exec -T postgres psql -X -U its_admin -d itsuite -tAc "$1"; }

# service_health SERVICE: healthy | running | the container's state | absent
service_health() {
  local svc state health
  IFS='|' read -r svc state health < <(dc ps -a --format '{{.Service}}|{{.State}}|{{.Health}}' "$1" 2>/dev/null; true) || true
  if [ -z "${svc:-}" ]; then echo absent
  elif [ "$state" = running ] && [ "$health" = healthy ]; then echo healthy
  elif [ "$state" = running ] && [ -z "$health" ]; then echo running
  else echo "$state${health:+ ($health)}"
  fi
}

# oneshot_exit SERVICE: the exit code of its last container, or "none".
oneshot_exit() {
  local id
  id="$(dc ps -aq "$1" 2>/dev/null | head -1)"
  [ -n "$id" ] || { echo none; return 0; }
  docker inspect -f '{{.State.ExitCode}}' "$id" 2>/dev/null || echo none
}

# ------------------------------------------------------- the shares

shares=()   # the five, when this run printed them; this shell's memory only
typed=()    # the three typed at step 3.3, kept for openbao-seal.sh
root=""
share=""

show_shares() {
  local i rule="================================================================================"
  printf '\n%s\n' "$rule"
  printf '%s\n' " OpenBao: the five unseal key shares and the root token. Printed ONCE, never again."
  printf '%s\n' " Each share to a different custodian, the root token to your password manager:"
  printf '%s\n' " all OFF this VPS. Three are needed; three lost is a lost vault."
  printf '%s\n' "$rule"
  for i in 0 1 2 3 4; do printf '  share %d     %s\n' "$((i + 1))" "${shares[$i]}"; done
  printf '  root token  %s\n' "$root"
  printf '%s\n\n' "$rule"
}

confirm_saved() {
  local answer=""
  while :; do
    read -r -p "Type SAVED once the five shares and the root token are stored off this VPS: " answer \
      || die "the input ended before SAVED" \
             "OpenBao is initialised and its shares were printed above, once: copy them now." \
             "./scripts/first-boot.sh asks for the root token and three shares when it runs again"
    [ "$answer" = SAVED ] && break
    note "type SAVED, in capitals, to go on"
  done
  # The screen and its scrollback: the shares stay only where you copied them.
  if [ -t 1 ]; then printf '\033[H\033[2J\033[3J'; fi
  ok "step 3.2: the shares and the root token are in your custody"
}

# read_share N: one share, hidden, into $share. When this run printed the
# five, it must be one of them; and never one typed already.
read_share() {
  local n="$1" wrong=0
  while :; do
    share=""
    IFS= read -r -s -p "Unseal key share $n of 3 (hidden): " share || [ -n "$share" ] \
      || die "the input ended before three shares" "./scripts/first-boot.sh asks for them again when it runs again"
    if [ -t 0 ]; then echo; fi
    share="${share//[[:space:]]/}"
    if [ -z "$share" ]; then note "that was empty: type the share"; continue; fi
    if [ "${#typed[@]}" -gt 0 ] && is_one_of "$share" "${typed[@]}"; then
      note "that share was typed already: type a different one"; continue
    fi
    if [ "${#shares[@]}" = 5 ] && ! is_one_of "$share" "${shares[@]}"; then
      wrong=$((wrong + 1))
      [ "$wrong" -lt 3 ] || die "three times not one of the five shares printed above" \
        "check your copies against what you stored, then run ./scripts/first-boot.sh again"
      note "that is not one of the five shares printed above: check your copy"; continue
    fi
    return 0
  done
}

# Each share reaches `bao` on stdin, as JSON, never in an argv.
submit_share() {
  printf '{"key":"%s"}' "$1" \
    | docker compose exec -T openbao bao write -format=json sys/unseal - >/dev/null 2>&1 || true
}
reset_unseal() {
  printf '{"reset":true}' \
    | docker compose exec -T openbao bao write -format=json sys/unseal - >/dev/null 2>&1 || true
}

unseal_with_three() {
  local round n
  for round in 1 2 3; do
    typed=()
    reset_unseal
    for n in 1 2 3; do
      read_share "$n"
      typed+=("$share")
      submit_share "$share"
    done
    share=""
    bao_state
    if [ "$bao_sealed" = false ]; then
      ok "step 3.3: OpenBao is unsealed: three shares, and your copies of them are good"
      return 0
    fi
    warn "OpenBao is still sealed: one of those three is not a share of this OpenBao (round $round of 3)"
  done
  die "OpenBao is still sealed after three rounds of three shares" \
      "check the copies with their custodians, then run ./scripts/first-boot.sh again"
}

read_root_token() {
  local t=""
  IFS= read -r -s -p "OpenBao root token, printed by step 3.2 (hidden): " t || [ -n "$t" ] \
    || die "the input ended before the root token" "run ./scripts/first-boot.sh again and paste it at the prompt"
  if [ -t 0 ]; then echo; fi
  t="${t//[[:space:]]/}"
  is_root_token "$t" || die "that is not this OpenBao's root token" \
    "run ./scripts/first-boot.sh again and paste the root token step 3.2 printed"
  root="$t"
}

start_from_zero=("start from zero, which DESTROYS this stack's volumes and everything in them:"
                 "docker compose down -v && rm .env" "./scripts/first-boot.sh")

# --------------------------------------------------------------- steps

check_state_before_anything() {
  say "Before anything: the state this host is in"
  local volumes
  if [ -e .env ]; then
    ok ".env exists: this is a run after step 1"
    return 0
  fi
  volumes="$(docker volume ls -q --filter label=com.docker.compose.project=it-suite 2>/dev/null | tr '\n' ' ' || true)"
  if [ -n "${volumes// /}" ]; then
    die "mixed state: this host has the volumes of an earlier first boot (${volumes% }) but no .env, and a new .env would not open them" \
        "if you have that first boot's .env (in its off-host custody), put it back as deploy/.env, mode 600, and run this again;" \
        "to start from zero instead, which DESTROYS those volumes and everything in them:" \
        "docker ps -aq --filter label=com.docker.compose.project=it-suite | xargs -r docker rm -f" \
        "docker volume ls -q --filter label=com.docker.compose.project=it-suite | xargs -r docker volume rm" \
        "./scripts/first-boot.sh"
  fi
  ok "no .env and no it-suite volume: a new host"
}

step_1() {
  say "Step 1 — .env"
  local in_env args
  if [ -e .env ]; then
    in_env="$(env_get ITS_PUBLIC_HOST)"
    if [ -n "$host" ] && [ "$host" != "$in_env" ]; then
      die "--host $host, but .env is for ${in_env:-no host}" "./scripts/first-boot.sh, without --host" "${start_from_zero[@]}"
    fi
    host="$in_env"
    [ -n "$host" ] || die ".env has no ITS_PUBLIC_HOST" "${start_from_zero[@]}"
    if [ -n "$email$ca_server" ]; then note "--acme-email and --acme-ca-server are for a new .env; this one stays as it is"; fi
    skip "step 1: .env exists (new-env.sh never overwrites one), for $host"
  else
    host="${host:-rmm.novieri.com}"
    args=(--host "$host")
    [ -z "$email" ] || args+=(--acme-email "$email")
    [ -z "$ca_server" ] || args+=(--acme-ca-server "$ca_server")
    act "./scripts/new-env.sh ${args[*]}"
    ./scripts/new-env.sh "${args[@]}"
  fi
  [ "$(stat -c %a .env)" = 600 ] || die ".env is mode $(stat -c %a .env), not 600: it holds every password of this deployment" "chmod 600 .env" "./scripts/first-boot.sh"
  follow_auto_unseal
}

step_1b() {
  say "Step 1b — tagged images"
  local head_tag tag v
  head_tag="$(git -C .. rev-parse --short HEAD)"
  tag="$(env_get IT_SUITE_VERSION)"
  if [ "$tag" = "$head_tag" ] && docker image inspect "itsuite/backend:$tag" "itsuite/web:$tag" >/dev/null 2>&1; then
    skip "step 1b: IT_SUITE_VERSION=$tag is this checkout's commit, and both images exist"
    return 0
  fi
  act "./scripts/build-images.sh, then IT_SUITE_VERSION=$head_tag in .env"
  ./scripts/build-images.sh </dev/null
  v="$(git -C .. rev-parse --short HEAD)" && sed -i '/^IT_SUITE_VERSION=/d' .env && echo "IT_SUITE_VERSION=$v" >> .env && grep '^IT_SUITE_VERSION=' .env
}

step_1c() {
  say "Step 1c — the pre-flight"
  ./scripts/preflight.sh </dev/null \
    || die "the pre-flight refused (above)" "fix each REFUSE line, then run ./scripts/first-boot.sh again"
}

step_2() {
  say "Step 2 — Postgres"
  local roles
  if [ "$(service_health postgres)" = healthy ]; then
    skip "step 2: postgres is up and healthy"
  else
    act "docker compose up -d --wait postgres"
    dc up -d --wait --wait-timeout 120 postgres || die "postgres did not become healthy" "docker compose logs --tail 50 postgres"
  fi
  roles="$(psql_admin "select count(*) from pg_roles where rolname in ('app_user', 'portal_user')" | tr -d '[:space:]')"
  [ "$roles" = 2 ] || die "postgres has no app_user and portal_user: its data directory is older than this .env, so postgres/init-app-user.sh never ran on it" \
    "docker compose down -v postgres" "./scripts/first-boot.sh"
  ok "step 2: postgres has app_user and portal_user"
}

step_3_1() {
  say "Step 3.1 — OpenBao"
  bao_state
  if [ "$bao_up" = yes ]; then
    skip "step 3.1: OpenBao is up"
  else
    act "docker compose up -d openbao"
    dc up -d openbao
    wait_bao_up
  fi
  ok "step 3.1: OpenBao answers: initialised $bao_init, sealed $bao_sealed, seal $bao_type"
  if [ "$bao_init" = false ] && { env_token_set ITS_OPENBAO_TOKEN || env_token_set OPENBAO_TOKEN; }; then
    die "mixed state: .env holds OpenBao tokens, but this OpenBao was never initialised: .env is from another first boot" "${start_from_zero[@]}"
  fi
}

step_3_2() {
  say "Step 3.2 — initialise OpenBao: the only copy of its shares"
  local init_out line
  if [ "$bao_init" = true ]; then
    skip "step 3.2: OpenBao is initialised, and this never initialises it again"
    return 0
  fi
  if [ ! -t 1 ] && [ "$allow_nontty" != 1 ]; then
    die "the output is not a terminal, and this step prints the five shares and the root token, once" \
        "run ./scripts/first-boot.sh in a terminal, with nothing redirected"
  fi
  act "bao operator init -key-shares=5 -key-threshold=3"
  init_out="$(dc exec -T openbao bao operator init -key-shares=5 -key-threshold=3)" \
    || die "bao operator init failed" "docker compose logs --tail 50 openbao"
  while IFS= read -r line; do
    case "$line" in
      "Unseal Key "[1-5]": "*) shares+=("${line#*: }") ;;
      "Initial Root Token: "*) root="${line#*: }" ;;
    esac
  done < <(printf '%s\n' "$init_out")
  if [ "${#shares[@]}" != 5 ] || [ -z "$root" ]; then
    printf '%s\n' "$init_out"
    die "bao operator init did not print five shares and a root token (its output is above: the only copy)" \
        "copy all of it off this VPS now; ./scripts/first-boot.sh then asks for the root token and three shares"
  fi
  init_out=""
  show_shares
  confirm_saved
}

step_3_3() {
  say "Step 3.3 — unseal"
  local root_in_env
  bao_state
  if [ "$bao_sealed" = false ]; then
    skip "step 3.3: OpenBao is unsealed"
  elif auto_unseal_on; then
    note "the auto-unseal is on: waiting for OpenBao to unseal itself"
    wait_unsealed || die "OpenBao is sealed although the auto-unseal is on: its key file is missing or wrong" \
      "sudo ./scripts/openbao-seal.sh status" "docker compose logs --tail 50 openbao" \
      "deploy/README.md, After a reboot or a restart, says how to put the key file back"
    ok "step 3.3: OpenBao unsealed itself (seal $bao_type)"
  else
    unseal_with_three
  fi
  if env_token_set OPENBAO_TOKEN; then
    root_in_env="$(env_get OPENBAO_TOKEN)"
    is_root_token "$root_in_env" || die "mixed state: OPENBAO_TOKEN in .env is not this OpenBao's root token: .env and OpenBao come from two first boots" "${start_from_zero[@]}"
    [ -n "$root" ] || root="$root_in_env"
  fi
}

step_3_4() {
  say "Step 3.4 — the runtime policies and their tokens; 3.5 — both in .env"
  local need_app=no need_gw=no app gw
  env_token_set ITS_OPENBAO_TOKEN || need_app=yes
  sudo test -s "$token_file" || need_gw=yes
  if [ "$need_app" = no ] && [ "$need_gw" = no ] && env_token_set OPENBAO_TOKEN; then
    skip "step 3.4: ITS_OPENBAO_TOKEN and OPENBAO_TOKEN are in .env, and $token_file exists"
    return 0
  fi
  [ -n "$root" ] || read_root_token
  act "bao policy write its-app and gateway-issuer; bao auth tune; the two tokens"
  bao_with "$root" bao policy write its-app /bao/its-app.hcl
  bao_with "$root" bao policy write gateway-issuer /bao/gateway-issuer.hcl
  bao_with "$root" bao auth tune -max-lease-ttl=87600h token/
  if [ "$need_gw" = yes ]; then
    gw="$(bao_with "$root" bao token create -orphan -policy=gateway-issuer -ttl=87600h -field=token)"
    [ -n "$gw" ] || die "OpenBao issued no gateway-issuer token" "docker compose logs --tail 50 openbao"
    sudo install -d -m 700 "$etc_dir"
    sudo install -m 600 /dev/null "$token_file"
    printf '%s\n' "$gw" | sudo tee "$token_file" >/dev/null
    gw=""
    ok "step 3.4: the gateway-issuer token is in $token_file (mode 0600, root's)"
  fi
  if [ "$need_app" = yes ]; then
    app="$(bao_with "$root" bao token create -orphan -policy=its-app -ttl=87600h -field=token)"
    [ -n "$app" ] || die "OpenBao issued no its-app token" "docker compose logs --tail 50 openbao"
    env_put ITS_OPENBAO_TOKEN "$app"
    app=""
  fi
  env_token_set OPENBAO_TOKEN || env_put OPENBAO_TOKEN "$root"
  [ "$(grep -c 'change-me' .env || true)" = 0 ] || die ".env still has a change-me value" "grep -n change-me .env"
  ok "step 3.5: ITS_OPENBAO_TOKEN (its-app) and OPENBAO_TOKEN (root) are in .env, two different values"
}

step_3_6() {
  say "Step 3.6 — the transit key and the PKI"
  local code
  if [ "$(oneshot_exit openbao-init)" = 0 ]; then
    skip "step 3.6: openbao-init exited 0 (provisioning complete)"
    return 0
  fi
  act "docker compose up openbao-init"
  dc up openbao-init
  code="$(oneshot_exit openbao-init)"
  [ "$code" = 0 ] || die "openbao-init exited $code" "docker compose logs openbao-init"
  ok "step 3.6: openbao provisioning complete"
}

enabled_now=no
auto_unseal() {
  say "OpenBao's auto-unseal, with a static key (GO-LIVE.md §4; decided 2026-11-27)"
  local n
  bao_state
  if auto_unseal_on && [ "$bao_type" = static ] && [ "$bao_sealed" = false ]; then
    skip "auto-unseal: on, and OpenBao is unsealed with its static key"
    return 0
  fi
  if [ "$bao_type" != shamir ] || [ "$bao_sealed" != false ]; then
    die "OpenBao reports seal $bao_type, sealed $bao_sealed: openbao-seal.sh enable needs it unsealed, with shares" \
        "sudo ./scripts/openbao-seal.sh status"
  fi
  if [ "${#typed[@]}" != 3 ]; then
    note "openbao-seal.sh needs three shares to move the seal, and this run holds none: type them"
    typed=()
    for n in 1 2 3; do read_share "$n"; typed+=("$share"); done
    share=""
  fi
  act "sudo ./scripts/openbao-seal.sh enable --key-file $key_file --yes, the three shares on its stdin"
  # env -u: openbao-seal.sh writes COMPOSE_FILE into .env, and an inherited
  # one would win over it and recreate OpenBao without the seal it migrates to.
  printf '%s\n' "${typed[@]}" | sudo env -u COMPOSE_FILE ./scripts/openbao-seal.sh enable --key-file "$key_file" --yes \
    || die "openbao-seal.sh enable failed (above, with its way back)" "sudo ./scripts/openbao-seal.sh status"
  typed=()
  auto_unseal_on || die "openbao-seal.sh enable did not put the auto-unseal overlay into .env" "sudo ./scripts/openbao-seal.sh status"
  follow_auto_unseal
  enabled_now=yes
}

restart_proof() {
  say "OpenBao restarts and unseals itself"
  if [ "$enabled_now" != yes ]; then
    skip "the restart that proves it ran when the auto-unseal was turned on"
    return 0
  fi
  act "docker compose restart openbao"
  dc restart openbao
  wait_unsealed || die "OpenBao came back sealed after a restart: the auto-unseal does not work" \
    "sudo ./scripts/openbao-seal.sh status" "docker compose logs --tail 50 openbao"
  [ "$bao_type" = static ] || die "OpenBao unsealed, but its seal is $bao_type, not static" "sudo ./scripts/openbao-seal.sh status"
  ok "OpenBao restarted and came back unsealed by itself (Seal Type static, Sealed false): no share was typed"
}

# ------------------------------------------------------------------ main

check_state_before_anything
step_1
step_1b
step_1c
step_2
step_3_1
step_3_2
step_3_3
step_3_4
step_3_6
auto_unseal
restart_proof
say "Steps 1 to 3.6 and the auto-unseal: done."

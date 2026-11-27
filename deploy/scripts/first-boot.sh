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
# .env, the audit key or a token file. A token it finds -- in .env, in the
# gateway-issuer token file -- counts as done only once this OpenBao knows it.
# Run again after `bao operator init`, it asks for what it no longer holds in
# memory: the root token (hidden) if .env does not hold the tokens yet, and
# three shares if OpenBao is sealed and the auto-unseal is not on yet, or if
# the auto-unseal is still to be turned on (then they are proved first:
# OpenBao is sealed with the root token, and the three unseal it). From step 4
# on OpenBao unseals itself, so a failure there is run again with no share.
#
# IT REFUSES a mixed state -- .env, OpenBao (or the volumes) and the token
# file from different first boots -- and says how to start from zero. It
# names a seal migration openbao-seal.sh left half-way, with the way to finish
# it or undo it. And it refuses to print the shares anywhere but a terminal:
# with its output or its input redirected it stops before `bao operator init`,
# unless ITS_FIRST_BOOT_ALLOW_NONTTY=1 (the CI's `deploy` job, whose log goes
# with its runner).
#
# SECRETS. None goes on a command line, into the environment of a child, a
# log, a temporary file or the shell history: each reaches `bao` and `sudo
# tee` on stdin. They are written where the runbook writes them: .env (0600),
# the gateway-issuer token file (0600, root) and openbao-seal.sh's key file
# (0400). It refuses to run under `bash -x`.
#
# WHEN IT FAILS it prints FAILED, what failed, and the commands to go on with,
# each on its own line and starting with a `cd` to deploy/, so they can be
# pasted from any folder; what is not a command is a `#` comment. Where there
# are two ways on, each is a block of its own, after a line that says to run
# ONE of them; and no line comes after one that asks for a password or a
# share, so a block pasted whole never types into a prompt.
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

# No name a caller exported in lowercase stays exported: every name this
# script keeps a value in is lowercase, and bash 5.2 hands a child even a
# `local` whose name the environment exported (measured 2026-11-27), so a
# caller exporting `root` or `share` would give the root token or a share to
# every docker, sudo and tee below. The proxy variables stay: docker builds
# read them. (tests/test_first_boot_script.py checks the names are lowercase.)
while IFS= read -r name; do
  case "$name" in
    [a-z]*_proxy) ;;
    [a-z]*) export -n "${name?}" ;;
  esac
done < <(compgen -e)

if [ -t 1 ]; then bold=$'\033[1m'; plain=$'\033[0m'; else bold=""; plain=""; fi
step="starting"
say()  { step="$*"; printf '\n%s%s%s\n' "$bold" "$*" "$plain"; }
ok()   { printf '  ok      %s\n' "$*"; }
skip() { printf '  skip    %s\n' "$*"; }
act()  { printf '  run     %s\n' "$*"; }
note() { printf '  note    %s\n' "$*"; }
warn() { printf '  WARNING %s\n' "$*" >&2; }

# Every command a FAILED block prints is run from deploy/: the block starts
# with the `cd` to it, so the commands work from wherever the operator is.
deploy_dir="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
printf -v deploy_q '%q' "$deploy_dir"
rerun="./scripts/first-boot.sh"

# report WHAT [LINE...] [--or LINE...]...: what failed, then what to do, on
# stderr. A LINE is a command, at the eight-space indent, to paste as it
# stands; or, when it starts with "# ", prose, printed as that shell comment,
# so that a block pasted whole runs none of it. Each way on starts with the
# `cd` to deploy/. With --or between them the ways on are alternatives: each
# is a block of its own, after a line that says to run ONE of them. In a way
# on, a line that may read the terminal (sudo, a hidden prompt, this script)
# is the last: pasted whole, nothing after it is typed into its prompt.
# A report pasted whole runs its ways on one after another, so every way on
# after the first, and every line that destroys or undoes, is ONE `&&` chain
# that starts with `guard`: it goes on only if the operator types its word.
# (tests/first_boot_support.py, failed_block_problems, checks all of it, and
# test_a_whole_report_pasted_runs_one_way_on_at_most pastes every report.)
report() {
  local line ways=1 cd_said=no
  printf '\nFAILED  %s\n' "$1" >&2
  shift
  for line in "$@"; do [ "$line" != --or ] || ways=$((ways + 1)); done
  if [ "$ways" -gt 1 ]; then
    printf '  # run ONE of the following, never more than one: each block below is one whole way on, to paste on its own\n\n' >&2
  fi
  for line in "$@"; do
    case "$line" in
      --or) printf '\n' >&2; cd_said=no ;;
      "# "*) printf '  %s\n' "$line" >&2 ;;
      *) if [ "$cd_said" = no ]; then printf '        cd %s\n' "$deploy_q" >&2; cd_said=yes; fi
         printf '        %s\n' "$line" >&2 ;;
    esac
  done
}
die() {
  trap - EXIT   # this is the report: the exit trap must not print a second one
  report "$@"
  exit 1
}
# guard WORD WHAT: the head of a chain, in the operator's shell, that goes on
# only if they type WORD at a prompt saying what the chain does: "This WHAT.
# Type WORD to go on". Pasted with the rest of a report, the `read` takes the
# next pasted line, or waits for the keyboard; either way, not WORD. WHAT has
# no single quote.
guard() { printf "read -r -p 'This %s. Type %s to go on: ' answer && [ \"\$answer\" = %s ]" "$2" "$1" "$1"; }
# The SAVED prompt is the one moment the five shares and the root token are on
# the screen and nowhere else. Whatever stops the run there -- the input
# ending, Ctrl-C, a TERM, the terminal hanging up -- says so, before anything
# clears the screen, and how to go on either way.
at_saved=no
custody_lost() {
  printf '\n' >&2   # off the prompt's line
  warn "the five shares and the root token on this screen are their only copy: copy them off this VPS before anything clears it"
  # Even the way that keeps going waits for a typed SAVED (review r4): pasted
  # with the rest of this report, it would clear their only copy.
  die "$1: OpenBao is initialised already, and the five shares and the root token on this screen are their only copy" \
      "# they are stored off this VPS: the line below asks you to type SAVED, then clears this screen and its scrollback, where the shares still are, and runs it again, which asks for the root token and three shares" \
      "$(guard SAVED "clears this screen and its scrollback, where the five shares and the root token still are, and runs first-boot.sh again: only once they are stored off this VPS") && printf '\033[H\033[2J\033[3J' && $rerun" \
      --or \
      "# they are not, and cannot be any more: three lost shares are a vault nobody can open, and on a first boot nothing is in it yet, so this host starts from zero" \
      "${start_from_zero[@]}"
}
# Anything that stops the run outside `die` -- a command that fails where no
# check waits for it, Ctrl-C -- ends here: FAILED, the step, and the way on.
on_exit() {
  local rc="$1"
  trap - EXIT
  [ "$rc" != 0 ] || return 0
  [ "$at_saved" != yes ] || custody_lost "stopped at the SAVED prompt (exit $rc), before the screen was cleared"
  report "$step: a command failed (exit $rc); what it printed is above, and running it again goes on from what is done" "$rerun"
  exit 1
}
trap 'on_exit $?' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

host="" email="" ca_server="" tls_cafile=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host) [ $# -ge 2 ] || die "--host needs a value" "$rerun --help"; host="$2"; shift 2 ;;
    --acme-email) [ $# -ge 2 ] || die "--acme-email needs a value" "$rerun --help"; email="$2"; shift 2 ;;
    --acme-ca-server) [ $# -ge 2 ] || die "--acme-ca-server needs a value" "$rerun --help"; ca_server="$2"; shift 2 ;;
    --tls-cafile) [ $# -ge 2 ] || die "--tls-cafile needs a file" "$rerun --help"; tls_cafile="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" "$rerun --help" ;;
  esac
done
# Resolved before the cd below: a relative path names the caller's directory.
if [ -n "$tls_cafile" ]; then
  [ -s "$tls_cafile" ] || die "--tls-cafile $tls_cafile is missing or empty" "$rerun --help"
  tls_cafile="$(readlink -f "$tls_cafile")"
fi
# The command that runs it again, with the options it was given.
rerun_kept=""
[ -z "$tls_cafile" ] || rerun_kept+=" --tls-cafile $(printf '%q' "$tls_cafile")"
rerun_new=""
[ -z "$host" ] || rerun_new+=" --host $(printf '%q' "$host")"
[ -z "$email" ] || rerun_new+=" --acme-email $(printf '%q' "$email")"
[ -z "$ca_server" ] || rerun_new+=" --acme-ca-server $(printf '%q' "$ca_server")"
rerun="./scripts/first-boot.sh$rerun_new$rerun_kept"

cd "$(dirname "$(readlink -f "$0")")/.."   # deploy/
etc_dir="${ITS_ETC_DIR:-/etc/it-suite}"
unit_dir="${ITS_SYSTEMD_DIR:-/etc/systemd/system}"
no_systemctl="${ITS_NO_SYSTEMCTL:-0}"
allow_nontty="${ITS_FIRST_BOOT_ALLOW_NONTTY:-0}"
token_file="$etc_dir/gateway-issuer.token"
key_file="$etc_dir/openbao-unseal/unseal.key"
printf -v token_file_q '%q' "$token_file"
printf -v key_file_q '%q' "$key_file"
# A FAILED block names openbao-seal.sh's view in prose: as a command line it
# would ask sudo for a password, and so be the last line of its block.
seal_status_note="# what openbao-seal.sh sees: sudo env -u COMPOSE_FILE ./scripts/openbao-seal.sh status"
command -v docker >/dev/null 2>&1 || die "docker is not on PATH" \
  "# prepare-host.sh installs Docker and puts you in the docker group, which applies from your next login: run it, log out, log in again, and run ./scripts/first-boot.sh again" \
  "sudo ./scripts/prepare-host.sh"

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

# bao_state sets bao_up (yes|no), bao_init, bao_sealed (true|false), bao_type
# and bao_migration (true|false, or empty if not said). `bao status` exits 0
# unsealed and 2 sealed, initialised or not, and prints its JSON either way;
# anything else is "not answering". (`bao operator init -status` is no help:
# it exits 2 for "not initialised" AND for a refused connection, measured with
# OpenBao 2.6.1 on 2026-11-27.)
bao_state() {
  local out rc=0
  out="$(dc exec -T openbao bao status -format=json 2>/dev/null)" || rc=$?
  bao_up=no bao_init="" bao_sealed="" bao_type="" bao_migration=""
  case "$rc" in 0|2) ;; *) return 0 ;; esac
  bao_init="$(printf '%s\n' "$out" | sed -n 's/^ *"initialized": *\(true\|false\).*/\1/p' | head -1)"
  bao_sealed="$(printf '%s\n' "$out" | sed -n 's/^ *"sealed": *\(true\|false\).*/\1/p' | head -1)"
  bao_type="$(printf '%s\n' "$out" | sed -n 's/^ *"type": *"\([a-z-]*\)".*/\1/p' | head -1)"
  bao_migration="$(printf '%s\n' "$out" | sed -n 's/^ *"migration": *\(true\|false\).*/\1/p' | head -1)"
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
  die "OpenBao does not answer after 90 s" "docker compose ps openbao" "docker compose logs --tail 50 openbao" "$rerun"
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

# bao_token_on_stdin COMMAND...: one command in the openbao container, run
# with the token that is this function's first line of stdin, which reaches
# it there in its environment, never in an argv. bao_with TOKEN COMMAND...:
# the same, TOKEN given.
bao_token_on_stdin() {
  # shellcheck disable=SC2016  # expanded by the container's shell
  docker compose exec -T openbao sh -c 'read -r BAO_TOKEN && export BAO_TOKEN && exec "$@"' sh "$@"
}
bao_with() {
  local token="$1"
  shift
  printf '%s\n' "$token" | bao_token_on_stdin "$@"
}

# token_policies: the policies of the token on stdin, as `"default","its-app"`,
# or a failure when this OpenBao does not know it. The lookup's JSON holds the
# token itself, and stays in this function's memory.
# No `| grep -q` under pipefail: grep leaving early is a SIGPIPE upstream, and a false "no".
token_policies() {
  local out
  out="$(bao_token_on_stdin bao token lookup -format=json 2>/dev/null)" || return 1
  out="${out//[[:space:]]/}"
  [[ $out == *'"policies":['* ]] || return 1
  out="${out#*\"policies\":[}"
  printf '%s\n' "${out%%]*}"
}
# token_has POLICY: the token on stdin is this OpenBao's, and has POLICY.
token_has() { local p; p="$(token_policies)" || return 1; [[ ",$p," == *",\"$1\","* ]]; }
is_root_token() { local p; p="$(printf '%s\n' "$1" | token_policies)" || return 1; [ "$p" = '"root"' ]; }
# lookup_failed WHAT: after a lookup of WHAT that failed, before the token is
# blamed (review r5): only an OpenBao that answers, unsealed, has judged it.
# One that does not answer is no proof the token is another's, and on that
# word the operator would start from zero, or delete a token file whose token
# stays valid for ten years with no copy. As for shares (review M5).
lookup_failed() {
  local why="did not answer when it was asked about $1"
  bao_state
  [ "$bao_up" = yes ] && [ "$bao_sealed" = false ] && return 0
  [ "$bao_up" != yes ] || why="is sealed, so it could not look up $1"
  die "OpenBao $why: that token is not judged, and nothing is to be deleted" \
      "docker compose ps openbao" "docker compose logs --tail 50 openbao" "$rerun"
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
typed=()    # three that unsealed OpenBao in this run, kept for openbao-seal.sh
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
  at_saved=yes
  while :; do
    read -r -p "Type SAVED once the five shares and the root token are stored off this VPS: " answer \
      || custody_lost "the input ended before SAVED"
    [ "$answer" = SAVED ] && break
    note "type SAVED, in capitals, to go on"
  done
  at_saved=no
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
      || die "the input ended before three shares" "# run again, and it asks for them again:" "$rerun"
    if [ -t 0 ]; then echo; fi
    share="${share//[[:space:]]/}"
    if [ -z "$share" ]; then note "that was empty: type the share"; continue; fi
    if [ "${#typed[@]}" -gt 0 ] && is_one_of "$share" "${typed[@]}"; then
      note "that share was typed already: type a different one"; continue
    fi
    if [ "${#shares[@]}" = 5 ] && ! is_one_of "$share" "${shares[@]}"; then
      wrong=$((wrong + 1))
      [ "$wrong" -lt 3 ] || die "three times not one of the five shares printed above" \
        "# your copies are not what was printed: check them against what you stored, then run it again" "$rerun" \
        "${lost_shares[@]}"
      note "that is not one of the five shares printed above: check your copy"; continue
    fi
    return 0
  done
}

# Each share reaches `bao` on stdin, as JSON, never in an argv. What it says
# back is not kept: after a round, `bao status` tells a wrong share from an
# OpenBao that stopped answering.
submit_share() {
  printf '{"key":"%s"}' "$1" \
    | docker compose exec -T openbao bao write -format=json sys/unseal - >/dev/null 2>&1 || true
}
reset_unseal() {
  printf '{"reset":true}' \
    | docker compose exec -T openbao bao write -format=json sys/unseal - >/dev/null 2>&1 || true
}
not_answering() {
  die "OpenBao stopped answering $1: that is not a wrong share, and no share was judged" \
      "docker compose ps openbao" "docker compose logs --tail 50 openbao" "$rerun"
}

# unseal_with_three OK: up to three rounds of three shares, each round from
# a clean start; OK is the line a round that unseals OpenBao prints. The three
# of a round are all typed before any is submitted; three already in $typed
# are the first round's. The three that unseal it stay in $typed:
# openbao-seal.sh needs three good ones.
unseal_with_three() {
  local said="$1" round n held=no
  [ "${#typed[@]}" != 3 ] || held=yes
  for round in 1 2 3; do
    bao_state
    [ "$bao_up" = yes ] || not_answering "before round $round of the unseal"
    if [ "$held" = yes ]; then
      held=no
    else
      typed=()
      for n in 1 2 3; do read_share "$n"; typed+=("$share"); done
    fi
    reset_unseal
    for share in "${typed[@]}"; do submit_share "$share"; done
    share=""
    bao_state
    [ "$bao_up" = yes ] || not_answering "while it was being unsealed"
    if [ "$bao_sealed" = false ]; then
      ok "$said"
      return 0
    fi
    warn "OpenBao is still sealed: one of those three is not a share of this OpenBao (round $round of 3)"
  done
  typed=()
  die "OpenBao is still sealed after three rounds of three shares" \
      "# the copies are wrong: check them with their custodians, then run it again" "$rerun" \
      "${lost_shares[@]}"
}

read_root_token() {
  local t=""
  IFS= read -r -s -p "OpenBao root token, printed by step 3.2 (hidden): " t || [ -n "$t" ] \
    || die "the input ended before the root token" "# run again, and paste it at the prompt:" "$rerun"
  if [ -t 0 ]; then echo; fi
  t="${t//[[:space:]]/}"
  is_root_token "$t" || { lookup_failed "the root token typed"; die "that is not this OpenBao's root token" \
    "# run again, and paste the root token step 3.2 printed:" "$rerun"; }
  root="$t"
}

# What an earlier first boot leaves outside deploy/: its gateway-issuer token,
# which no new OpenBao knows, and its auto-unseal key, which openbao-seal.sh
# would reuse for the new one (it keeps any 32-byte key it finds).
# Starting from zero is one line behind a typed DESTROY (review I1, round 3):
# nothing of it runs unless the operator types the word, pasted or not. The
# removal runs the script again on the same line: sudo may ask for a password.
# `down -v` removes only the volumes Compose labelled; a restore that made
# them with `docker volume create` left them unlabelled, and `down -v` skips
# those (rehearsal of 2026-11-27), so every volume named for this stack goes
# by its name too.
earlier_prose="with them go the earlier first boot's gateway-issuer token and auto-unseal key; keep that key's off-host copy only as long as you keep backups of the vault it unsealed, or openbao-seal.sh would reuse it for the new one"
by_name="docker volume ls -q | sed -n '/^it-suite_/p' | xargs -r docker volume rm"
start_from_zero=(
  "# to start from zero: the line below asks you to type DESTROY, then DESTROYS this stack's volumes and everything in them, and .env; $earlier_prose"
  "$(guard DESTROY "DESTROYS the volumes of this stack and everything in them, deletes .env, and deletes the gateway-issuer token and auto-unseal key files of the earlier first boot") && docker compose down -v && rm .env && $by_name && sudo rm -f $token_file_q $key_file_q && $rerun")
# The other way on when shares do not unseal: lost, they are a vault nobody
# can open, and on a first boot there is nothing in it yet.
lost_shares=(--or
  "# the shares are lost: OpenBao cannot be opened without three of them, ever; on a first boot nothing is stored in it yet, and this host starts from zero"
  "${start_from_zero[@]}")

# A seal migration openbao-seal.sh enable left half-way: it writes the overlay
# into .env and recreates OpenBao in seal-migration mode BEFORE it reads a
# share, and it cannot be run again over that .env (it refuses a COMPOSE_FILE
# it did not just write). The way back is its own recover_help's.
pending_migration() {
  die "OpenBao is half-way through the seal migration openbao-seal.sh enable started: .env has the auto-unseal overlay, and OpenBao (seal ${bao_type:-unknown}, migration ${bao_migration:-not said}) still waits for three shares to move to its static key. The key file is not the problem." \
    "# to finish the migration: the line below runs three prompts in turn, and each asks for one share (hidden); then it restarts OpenBao, which proves it unseals itself, and runs this again. Paste it on its own; if a share was wrong, paste it again" \
    "# afterwards, as openbao-seal.sh says when it finishes one itself: copy $key_file OFF this host, stored apart from any openbao-data backup; keep the five shares, which are recovery keys now; and keep its Shamir-sealed snapshot under /var/backups/it-suite/openbao-seal/ until that copy has been compared with this host's (sha256sum, on both), then delete it" \
    "$seal_status_note" \
    "for n in 1 2 3; do docker compose exec openbao bao operator unseal -migrate; done && docker compose restart openbao && $rerun" \
    --or \
    "# to undo it, back to the shares alone, as openbao-seal.sh's own way back does: the line below asks you to type UNDO first; the run after it proves three shares and moves the seal again" \
    "$(guard UNDO "undoes the seal migration: it takes the auto-unseal lines out of .env and recreates OpenBao without its static seal, to be unsealed with shares alone") && sed -i '/^COMPOSE_FILE=/d;/^OPENBAO_UNSEAL_KEY_FILE=/d;/^OPENBAO_SEAL_STANZA=/d' .env && docker compose up -d --no-deps openbao && $rerun"
}

# --------------------------------------------------------------- steps

check_state_before_anything() {
  say "Before anything: the state this host is in"
  local labelled all volumes error f found=() removal
  if [ -e .env ]; then
    ok ".env exists: this is a run after step 1"
    return 0
  fi
  # This stack's volumes: those Compose labelled for it, and every one named
  # for it, which a restore's `docker volume create` makes with no label.
  if ! labelled="$(docker volume ls -q --filter label=com.docker.compose.project=it-suite 2>/dev/null)" \
     || ! all="$(docker volume ls -q 2>/dev/null)"; then
    error="$(docker volume ls -q 2>&1 >/dev/null || true)"
    die "docker volume ls failed, so this host's state cannot be read: ${error//$'\n'/ }" \
        "# if it says permission denied, the docker group applies from your next login: log out, log in again, and run:" \
        "$rerun"
  fi
  volumes="$({ printf '%s\n' "$labelled"; printf '%s\n' "$all" | sed -n '/^it-suite_/p'; } | sed '/^$/d' | sort -u)"
  volumes="${volumes//$'\n'/ }"
  if [ -n "${volumes// /}" ]; then
    die "mixed state: this host has the volumes of an earlier first boot (${volumes% }) but no .env, and a new .env would not open them" \
        "# that first boot's .env is in its off-host custody: copy it back to $deploy_dir/.env, then" \
        "chmod 600 .env" "$rerun" \
        --or \
        "# it is not: start from zero; the line below asks you to type DESTROY, then DESTROYS those volumes, everything in them and the containers that hold them; $earlier_prose" \
        "$(guard DESTROY "DESTROYS the it-suite containers and volumes and everything in them, and deletes the gateway-issuer token and auto-unseal key files of the earlier first boot") && docker ps -aq --filter label=com.docker.compose.project=it-suite | xargs -r docker rm -f && docker volume ls -q --filter label=com.docker.compose.project=it-suite | xargs -r docker volume rm && $by_name && sudo rm -f $token_file_q $key_file_q && $rerun"
  fi
  for f in "$token_file" "$key_file"; do
    if sudo test -e "$f"; then found+=("$(printf '%q' "$f")"); fi
  done
  if [ "${#found[@]}" -gt 0 ]; then
    removal="sudo rm -f ${found[*]} && $rerun"
    case " ${found[*]} " in
      *" $key_file_q "*) removal="$(guard DELETE "deletes the auto-unseal key file of an earlier first boot, and its gateway-issuer token file if one is listed") && $removal" ;;
    esac
    die "mixed state: no .env and no it-suite volume, but this host keeps files of an earlier first boot (${found[*]}): no OpenBao here knows them" \
        "# no OpenBao on this host knows them, and the new first boot makes its own; keep an auto-unseal key's off-host copy only as long as you keep backups of the vault it unsealed, or openbao-seal.sh would reuse it for the new one; deleting the key asks you to type DELETE first" \
        "$removal"
  fi
  ok "no .env, no it-suite volume and no file of an earlier first boot: a new host"
}

step_1() {
  say "Step 1 — .env"
  local in_env args
  if [ -e .env ]; then
    in_env="$(env_get ITS_PUBLIC_HOST)"
    if [ -n "$host" ] && [ "$host" != "$in_env" ]; then
      die "--host $host, but .env is for ${in_env:-no host}" \
          "# .env decides the host once it exists: run it without --host" \
          "./scripts/first-boot.sh$rerun_kept" --or "${start_from_zero[@]}"
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
  [ "$(stat -c %a .env)" = 600 ] || die ".env is mode $(stat -c %a .env), not 600: it holds every password of this deployment" "chmod 600 .env" "$rerun"
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
  ./scripts/build-images.sh </dev/null || die "build-images.sh failed (above)" "$rerun"
  v="$(git -C .. rev-parse --short HEAD)" && sed -i '/^IT_SUITE_VERSION=/d' .env && echo "IT_SUITE_VERSION=$v" >> .env && grep '^IT_SUITE_VERSION=' .env
}

step_1c() {
  say "Step 1c — the pre-flight"
  ./scripts/preflight.sh </dev/null \
    || die "the pre-flight refused (above)" "# fix each REFUSE line above, then run it again:" "$rerun"
}

step_2() {
  say "Step 2 — Postgres"
  local roles
  if [ "$(service_health postgres)" = healthy ]; then
    skip "step 2: postgres is up and healthy"
  else
    act "docker compose up -d --wait --wait-timeout 120 postgres"
    dc up -d --wait --wait-timeout 120 postgres || die "postgres did not become healthy" "docker compose logs --tail 50 postgres" "$rerun"
  fi
  roles="$(psql_admin "select count(*) from pg_roles where rolname in ('app_user', 'portal_user')" | tr -d '[:space:]')"
  [ "$roles" = 2 ] || die "postgres has no app_user and portal_user: its data directory is older than this .env, so postgres/init-app-user.sh never ran on it" \
    "# remove postgres and its volume -- only that one, which DESTROYS what is in it -- and run it again; the line below asks you to type DESTROY first" \
    "$(guard DESTROY "DESTROYS the postgres volume it-suite_postgres-data and everything in it") && docker compose rm -s -f postgres && docker volume rm it-suite_postgres-data && $rerun"
  ok "step 2: postgres has app_user and portal_user"
}

step_3_1() {
  say "Step 3.1 — OpenBao"
  bao_state
  if [ "$bao_up" = yes ]; then
    skip "step 3.1: OpenBao is up"
  else
    act "docker compose up -d openbao"
    if ! dc up -d openbao; then
      if auto_unseal_on; then
        die "docker compose up -d openbao failed (above): with the auto-unseal on, its key file ($key_file) is the first suspect" \
            "$seal_status_note" \
            "# deploy/README.md, After a reboot or a restart, says how to put the key file back; then run it again" \
            "docker compose logs --tail 50 openbao" "$rerun"
      fi
      die "docker compose up -d openbao failed (above)" "docker compose logs --tail 50 openbao" "$rerun"
    fi
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
  if [ "$allow_nontty" != 1 ]; then
    [ -t 1 ] || die "the output is not a terminal, and this step prints the five shares and the root token, once" \
                    "# run it in a terminal, with nothing redirected:" "$rerun"
    [ -t 0 ] || die "the input is not a terminal: SAVED must be typed by the person who saw the five shares, before the screen is cleared" \
                    "# run it in a terminal, with nothing redirected:" "$rerun"
  fi
  act "bao operator init -key-shares=5 -key-threshold=3"
  init_out="$(dc exec -T openbao bao operator init -key-shares=5 -key-threshold=3)" \
    || die "bao operator init failed" "docker compose logs --tail 50 openbao" "$rerun"
  while IFS= read -r line; do
    case "$line" in
      "Unseal Key "[1-5]": "*) shares+=("${line#*: }") ;;
      "Initial Root Token: "*) root="${line#*: }" ;;
    esac
  done < <(printf '%s\n' "$init_out")
  if [ "${#shares[@]}" != 5 ] || [ -z "$root" ]; then
    printf '%s\n' "$init_out"
    die "bao operator init did not print five shares and a root token (its output is above: the only copy)" \
        "# copy all of it off this VPS now; run again, and it asks for the root token and three shares:" "$rerun"
  fi
  init_out=""
  show_shares
  confirm_saved
}

step_3_3() {
  say "Step 3.3 — unseal"
  local root_in_env
  bao_state
  [ "$bao_up" = yes ] || wait_bao_up
  if [ "$bao_sealed" = false ]; then
    skip "step 3.3: OpenBao is unsealed"
  elif auto_unseal_on && { [ "$bao_migration" = true ] || [ "$bao_type" != static ]; }; then
    pending_migration
  elif [ "$bao_migration" = true ]; then
    die "OpenBao still waits for a seal migration, but .env no longer has the auto-unseal overlay: the undo of a migration left half-way is half done" \
        "# OpenBao has to be recreated from the .env as it is now: the line below asks you to type UNDO first" \
        "$(guard UNDO "finishes the undo: it recreates OpenBao from .env as it is now, without its static seal, to be unsealed with shares alone") && docker compose up -d --no-deps openbao && $rerun"
  elif auto_unseal_on; then
    note "the auto-unseal is on: waiting for OpenBao to unseal itself"
    wait_unsealed || die "OpenBao is sealed although the auto-unseal is on, with its static key: its key file is missing or wrong" \
      "$seal_status_note" \
      "# deploy/README.md, After a reboot or a restart, says how to put the key file back; then run it again" \
      "docker compose logs --tail 50 openbao" "$rerun"
    ok "step 3.3: OpenBao unsealed itself (seal $bao_type)"
  else
    unseal_with_three "step 3.3: OpenBao is unsealed: three shares, and your copies of them are good"
  fi
  if env_token_set OPENBAO_TOKEN; then
    root_in_env="$(env_get OPENBAO_TOKEN)"
    is_root_token "$root_in_env" || { lookup_failed "OPENBAO_TOKEN in .env"
      die "mixed state: OPENBAO_TOKEN in .env is not this OpenBao's root token: .env and OpenBao come from two first boots" "${start_from_zero[@]}"; }
    [ -n "$root" ] || root="$root_in_env"
  fi
}

step_3_4() {
  say "Step 3.4 — the runtime policies and their tokens; 3.5 — both in .env"
  local need_app=yes need_gw=yes app gw made=() what
  # A token found counts only once this OpenBao knows it (review I1): each
  # reaches `bao token lookup` on stdin.
  if env_token_set ITS_OPENBAO_TOKEN; then
    env_get ITS_OPENBAO_TOKEN | token_has its-app || { lookup_failed "ITS_OPENBAO_TOKEN in .env"
      die "mixed state: ITS_OPENBAO_TOKEN in .env is not an its-app token of this OpenBao: .env and OpenBao come from two first boots" "${start_from_zero[@]}"; }
    need_app=no
  fi
  if sudo test -s "$token_file"; then
    sudo cat "$token_file" | token_has gateway-issuer || { lookup_failed "the token in $token_file"
      die "mixed state: $token_file holds a token this OpenBao does not know, or not a gateway-issuer one: it is from another first boot" \
          "# this OpenBao issues a new one once the file is gone" "sudo rm -f $token_file_q && $rerun"; }
    need_gw=no
  fi
  if [ "$need_app" = no ] && [ "$need_gw" = no ] && env_token_set OPENBAO_TOKEN; then
    skip "step 3.4: ITS_OPENBAO_TOKEN (its-app), OPENBAO_TOKEN (root) and $token_file (gateway-issuer) are tokens of this OpenBao"
    return 0
  fi
  [ -n "$root" ] || read_root_token
  if [ "$need_app" = yes ] || [ "$need_gw" = yes ]; then
    [ "$need_gw" = no ] || made+=(gateway-issuer)
    [ "$need_app" = no ] || made+=(its-app)
    what="a new token for ${made[0]}"
    [ "${#made[@]}" = 1 ] || what="new tokens for ${made[0]} and ${made[1]}"
    act "bao policy write its-app and gateway-issuer; bao auth tune -max-lease-ttl=87600h token/; $what"
    if ! { bao_with "$root" bao policy write its-app /bao/its-app.hcl \
           && bao_with "$root" bao policy write gateway-issuer /bao/gateway-issuer.hcl \
           && bao_with "$root" bao auth tune -max-lease-ttl=87600h token/; }; then
      die "a bao policy write or auth tune failed (above)" "docker compose logs --tail 50 openbao" "$rerun"
    fi
  fi
  if [ "$need_gw" = yes ]; then
    gw="$(bao_with "$root" bao token create -orphan -policy=gateway-issuer -ttl=87600h -field=token)" || gw=""
    [ -n "$gw" ] || die "OpenBao issued no gateway-issuer token" "docker compose logs --tail 50 openbao" "$rerun"
    { sudo install -d -m 700 "$etc_dir" && sudo install -m 600 /dev/null "$token_file"; } \
      || die "$token_file could not be made (above)" "$rerun"
    printf '%s\n' "$gw" | sudo tee "$token_file" >/dev/null || die "$token_file could not be written (above)" "$rerun"
    gw=""
    sudo cat "$token_file" | token_has gateway-issuer || { lookup_failed "the token just written to $token_file"
      die "$token_file does not hold the gateway-issuer token just issued" "sudo rm -f $token_file_q && $rerun"; }
    [ "$(sudo stat -c %a "$token_file")" = 600 ] || die "$token_file is not mode 600" "sudo chmod 600 $token_file_q && $rerun"
    ok "step 3.4: $token_file (mode 600, written with sudo) holds this OpenBao's gateway-issuer token"
  fi
  if [ "$need_app" = yes ]; then
    app="$(bao_with "$root" bao token create -orphan -policy=its-app -ttl=87600h -field=token)" || app=""
    [ -n "$app" ] || die "OpenBao issued no its-app token" "docker compose logs --tail 50 openbao" "$rerun"
    env_put ITS_OPENBAO_TOKEN "$app"
    app=""
  fi
  if ! env_token_set OPENBAO_TOKEN; then
    [ "$need_app" = yes ] || [ "$need_gw" = yes ] || act "OPENBAO_TOKEN (the root token) into .env"
    env_put OPENBAO_TOKEN "$root"
  fi
  [ "$(grep -c 'change-me' .env || true)" = 0 ] || die ".env still has a change-me value" "grep -n change-me .env" "$rerun"
  # Two different values, as the ok line says, follow from these two checks:
  # a root token's policies are exactly "root", and that is not its-app.
  if ! { env_get ITS_OPENBAO_TOKEN | token_has its-app && is_root_token "$(env_get OPENBAO_TOKEN)"; }; then
    lookup_failed "ITS_OPENBAO_TOKEN and OPENBAO_TOKEN in .env"
    die "ITS_OPENBAO_TOKEN and OPENBAO_TOKEN in .env are not this OpenBao's its-app and root tokens, two different values" "${start_from_zero[@]}"
  fi
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
  dc up openbao-init || die "docker compose up openbao-init failed (above)" "docker compose logs openbao-init" "$rerun"
  code="$(oneshot_exit openbao-init)"
  [ "$code" = 0 ] || die "openbao-init exited $code" "docker compose logs openbao-init" "$rerun"
  ok "step 3.6: openbao provisioning complete"
}

enabled_now=no
auto_unseal() {
  say "OpenBao's auto-unseal, with a static key (GO-LIVE.md §4; decided 2026-11-27)"
  local n answer
  bao_state
  if auto_unseal_on && [ "$bao_type" = static ] && [ "$bao_sealed" = false ]; then
    skip "auto-unseal: on, and OpenBao is unsealed with its static key"
    return 0
  fi
  if [ "$bao_type" != shamir ] || [ "$bao_sealed" != false ]; then
    die "OpenBao reports seal ${bao_type:-unknown}, sealed ${bao_sealed:-unknown}: openbao-seal.sh enable needs it unsealed, with shares" \
        "$seal_status_note" "$rerun"
  fi
  # The three must be good BEFORE openbao-seal.sh gets them: it writes .env
  # and recreates OpenBao in seal-migration mode first, and a wrong one then
  # stops it half-way (review I2). Three that unsealed OpenBao in this run
  # are; three typed now are proved the same way: OpenBao sealed again with
  # the root token, and unsealed with them, three rounds at most. The three
  # are typed, and the seal confirmed, BEFORE it (review m1): without the
  # shares at hand, the operator stops at a prompt and nothing has changed.
  if [ "${#typed[@]}" != 3 ]; then
    note "openbao-seal.sh needs three shares to move the seal, and this run holds none: type them"
    note "they are proved first: once the three are typed and you confirm, the root token seals OpenBao and the three unseal it, so a wrong one is asked again here, never inside the seal migration. OpenBao stays sealed until three good shares unseal it: without them at hand, stop at the first prompt (Ctrl-C), and nothing changes"
    [ -n "$root" ] || read_root_token
    typed=()
    for n in 1 2 3; do read_share "$n"; typed+=("$share"); done
    share=""
    answer=""
    read -r -p "OpenBao is sealed next, and only three good shares unseal it: type SEAL to seal it now and unseal it with these three (anything else stops here, and nothing changes): " answer \
      || answer=""
    if [ "$answer" != SEAL ]; then
      typed=()
      die "stopped before the seal, as asked: nothing was changed, and OpenBao is unsealed as it was" \
          "# run it again with three shares at hand" "$rerun"
    fi
    act "bao operator seal, with the root token on its stdin"
    bao_with "$root" bao operator seal >/dev/null || die "bao operator seal failed (above)" "docker compose logs --tail 50 openbao" "$rerun"
    bao_state
    [ "$bao_sealed" = true ] || die "OpenBao does not report itself sealed after bao operator seal" "docker compose logs --tail 50 openbao" "$rerun"
    unseal_with_three "auto-unseal: the three shares unsealed OpenBao, so they are the ones openbao-seal.sh needs"
  fi
  act "sudo ./scripts/openbao-seal.sh enable --key-file $key_file --yes, the three shares on its stdin"
  # env -u: openbao-seal.sh writes COMPOSE_FILE into .env, and an inherited
  # one would win over it and recreate OpenBao without the seal it migrates to.
  printf '%s\n' "${typed[@]}" | sudo env -u COMPOSE_FILE ./scripts/openbao-seal.sh enable --key-file "$key_file" --yes \
    || die "openbao-seal.sh enable failed (above, with its way back)" \
           "# run it again: if openbao-seal.sh stopped half-way through the seal migration, step 3.3 says how to finish it or undo it" \
           "$seal_status_note" "$rerun"
  typed=()
  auto_unseal_on || die "openbao-seal.sh enable did not put the auto-unseal overlay into .env" "$seal_status_note" "$rerun"
  follow_auto_unseal
  enabled_now=yes
}

restart_proof() {
  say "OpenBao restarts and unseals itself"
  if [ "$enabled_now" != yes ]; then
    skip "the restart that proves it: this run did not turn the auto-unseal on, so it does not restart OpenBao"
    return 0
  fi
  act "docker compose restart openbao"
  dc restart openbao || die "docker compose restart openbao failed (above)" "docker compose logs --tail 50 openbao" "$rerun"
  wait_unsealed || die "OpenBao came back sealed after a restart: the auto-unseal does not work" \
    "$seal_status_note" "docker compose logs --tail 50 openbao" "$rerun"
  [ "$bao_type" = static ] || die "OpenBao unsealed, but its seal is $bao_type, not static" "$seal_status_note" "$rerun"
  ok "OpenBao restarted and came back unsealed by itself (Seal Type static, Sealed false): no share was typed"
}

step_4() {
  say "Step 4 — migrations"
  local newest current code
  newest="$(find ../backend/migrations/versions -maxdepth 1 -name '[0-9][0-9][0-9][0-9]_*.py' -printf '%f\n' | cut -c1-4 | sort | tail -1)"
  [ -n "$newest" ] || die "no revision file in ../backend/migrations/versions: this is not a checkout of the repository" \
    "# deploy/ has to be the one in a whole checkout of the repository (deploy/README.md, P6), with backend/ beside it; from there, run it again:" \
    "$rerun"
  current="$(psql_admin 'select version_num from alembic_version' 2>/dev/null | tr -d '[:space:]' || true)"
  if [ "$current" = "$newest" ]; then
    skip "step 4: the schema is at $newest, this checkout's newest revision"
    return 0
  fi
  act "docker compose up migrate"
  # What decides is migrate's own exit code, next: whether compose's follows it
  # is not measured (review of Task 3, (a)).
  dc up migrate || true
  code="$(oneshot_exit migrate)"
  [ "$code" = 0 ] || die "migrate exited $code" "docker compose logs migrate" "$rerun"
  current="$(psql_admin 'select version_num from alembic_version' | tr -d '[:space:]')"
  [ "$current" = "$newest" ] || die "after migrate the schema is at ${current:-nothing}, not $newest" "docker compose logs migrate" "$rerun"
  ok "step 4: the schema is at $current"
}

# Step 4b's question, what postgres/grant-app-privileges.sql leaves: app_user
# reads (its GRANT on every table, here on devices) and holds none of what it
# REVOKEs. The RBAC catalog is the one only that file narrows: migrate leaves
# init-app-user.sh's default privileges, all four on every table, and the two
# logs narrowed by migrations 0006 and 0009 (review of Task 3, I1). A list of
# privileges is true if ANY is held.
narrowed_query="select has_table_privilege('app_user', 'devices', 'SELECT')"
narrowed_query+=" and not has_table_privilege('app_user', 'permissions', 'INSERT, UPDATE, DELETE')"
narrowed_query+=" and not has_table_privilege('app_user', 'role_permissions', 'INSERT, UPDATE, DELETE')"
narrowed_query+=" and not has_table_privilege('app_user', 'audit_events', 'UPDATE, DELETE')"
narrowed_query+=" and not has_table_privilege('app_user', 'security_events', 'UPDATE, DELETE')"
narrowed_said="app_user reads devices, cannot write the RBAC catalog (permissions, role_permissions), and cannot rewrite audit_events or security_events"

step_4b() {
  say "Step 4b — app_user's table privileges"
  local narrow code query="$narrowed_query"
  narrow="$(psql_admin "$query" 2>/dev/null | tr -d '[:space:]' || true)"
  if [ "$narrow" = t ]; then
    skip "step 4b: $narrowed_said"
    return 0
  fi
  act "docker compose up postgres-grants"
  dc up postgres-grants || true   # its own exit code decides, next (as for migrate)
  code="$(oneshot_exit postgres-grants)"
  [ "$code" = 0 ] || die "postgres-grants exited $code" "docker compose logs postgres-grants" "$rerun"
  narrow="$(psql_admin "$query" | tr -d '[:space:]')"
  [ "$narrow" = t ] || die "after postgres-grants, app_user is not narrowed as postgres/grant-app-privileges.sql says" "docker compose logs postgres-grants" "$rerun"
  ok "step 4b: $narrowed_said"
}

step_5() {
  say "Step 5 — the audit HMAC key"
  if dc run --rm --no-deps --entrypoint sh api -c 'test -s /var/lib/its/secrets/audit-hmac.key' >/dev/null 2>&1; then
    skip "step 5: the audit HMAC key exists, and it is never replaced"
  else
    act "32 random bytes into /var/lib/its/secrets/audit-hmac.key, mode 600"
    # `set -C`: the shell refuses to write over a key that is already there.
    # Then it is there, and the check above did not find a key in it: empty
    # (the api refuses an empty key, backend/app/core/audit.py, so nothing was
    # ever signed with one), or unreadable. Only an empty one is removed, and
    # only behind a typed DELETE (review of Task 3, m5).
    if ! dc run --rm --no-deps --entrypoint sh api -c \
         'set -C && head -c 32 /dev/urandom > /var/lib/its/secrets/audit-hmac.key && chmod 600 /var/lib/its/secrets/audit-hmac.key'; then
      dc run --rm --no-deps --entrypoint sh api -c 'ls -l /var/lib/its/secrets/audit-hmac.key' || true
      die "/var/lib/its/secrets/audit-hmac.key is there, so set -C refused to write a new one, but the check before found no key in it: it is empty, or it could not be read (its ls -l is above)" \
          "# a key that is not empty is the audit chain's, and is never removed; one of 0 bytes never signed a record (the api refuses an empty key): the line below asks you to type DELETE, removes the file only if it is still empty, and runs this again" \
          "$(guard DELETE "deletes the audit HMAC key file, only if it is empty") && docker compose run --rm --no-deps --entrypoint sh api -c 'test -f /var/lib/its/secrets/audit-hmac.key && test ! -s /var/lib/its/secrets/audit-hmac.key && rm /var/lib/its/secrets/audit-hmac.key' && $rerun"
    fi
  fi
  dc run --rm --no-deps --entrypoint sh api -c 'ls -l /var/lib/its/secrets/audit-hmac.key'
}

step_6() {
  say "Step 6 — the gateway's certificate, and the client CA"
  if dc run --rm --no-deps --entrypoint sh gateway -c \
       'test -s /var/lib/its/tls/gateway.crt && test -s /var/lib/its/tls/gateway.key && test -s /var/lib/its/ca/root.crt' >/dev/null 2>&1; then
    skip "step 6: the gateway's certificate and the client CA exist (step 6b's timer renews them)"
    return 0
  fi
  act "issue-gateway-cert.py $host, the gateway-issuer token on its stdin from $token_file"
  if ! sudo cat "$token_file" | docker compose run --rm --no-deps -T \
         -v "$PWD/scripts/issue-gateway-cert.py:/tmp/issue-gateway-cert.py:ro" \
         --entrypoint python gateway /tmp/issue-gateway-cert.py "$host"; then
    die "the gateway's certificate was not issued (above)" "docker compose logs --tail 50 openbao" "$rerun"
  fi
}

# Step 6b's two units, exactly as deploy/README.md's appendix writes them.
gateway_cert_service() {
  # shellcheck disable=SC2016,SC1003  # $PWD and the backslashes are the unit's
  printf '%s\n' \
    '[Unit]' \
    'Description=Renew the it-suite gateway server certificate' \
    '[Service]' \
    'Type=oneshot' \
    "WorkingDirectory=$deploy_dir" \
    '# The token arrives on STDIN from a 0600 file, never as an argument. `-T` is' \
    '# what keeps stdin available to redirect.' \
    "ExecStart=/bin/bash -lc 'docker compose run --rm --no-deps -T \\" \
    '  -v "$PWD/scripts/issue-gateway-cert.py:/tmp/issue-gateway-cert.py:ro" \' \
    "  --entrypoint python gateway /tmp/issue-gateway-cert.py $host \\" \
    "  < $token_file \\" \
    "  && docker compose restart gateway'"
}
gateway_cert_timer() {
  printf '%s\n' \
    '[Unit]' \
    'Description=Monthly it-suite gateway certificate renewal' \
    '[Timer]' \
    'OnCalendar=monthly' \
    'Persistent=true' \
    '[Install]' \
    'WantedBy=timers.target'
}

timer_enabled() {
  [ "$no_systemctl" = 1 ] || [ "$(systemctl is-enabled its-gateway-cert.timer 2>/dev/null || true)" = enabled ]
}

step_6b() {
  say "Step 6b — renew the gateway's certificate every month"
  local service="$unit_dir/its-gateway-cert.service" timer="$unit_dir/its-gateway-cert.timer"
  local enabled=", and the timer is enabled"
  [ "$no_systemctl" != 1 ] || enabled=" (ITS_NO_SYSTEMCTL=1: systemd not asked)"
  if [ "$(cat "$service" 2>/dev/null || true)" = "$(gateway_cert_service)" ] \
     && [ "$(cat "$timer" 2>/dev/null || true)" = "$(gateway_cert_timer)" ] && timer_enabled; then
    skip "step 6b: its-gateway-cert.service and .timer are as the runbook writes them$enabled"
    return 0
  fi
  act "its-gateway-cert.service and its-gateway-cert.timer in $unit_dir"
  sudo install -d -m 755 "$unit_dir"
  gateway_cert_service | sudo tee "$service" >/dev/null
  gateway_cert_timer | sudo tee "$timer" >/dev/null
  if [ "$no_systemctl" = 1 ]; then
    note "ITS_NO_SYSTEMCTL=1: units written, systemd not touched"
  else
    sudo systemctl daemon-reload
    sudo systemctl enable --now its-gateway-cert.timer
    sudo systemctl list-timers its-gateway-cert.timer --no-pager
  fi
}

oneshots=" migrate postgres-grants openbao-init "

# stack_state sets stack (ok | wait | fail), stack_text and stack_names.
stack_state() {
  local svc state health code
  stack=ok stack_text="" stack_names=""
  while IFS='|' read -r svc state health code; do
    [ -n "$svc" ] || continue
    case "$state" in
      exited)
        if [[ "$oneshots" == *" $svc "* ]] && [ "$code" = 0 ]; then continue; fi
        stack="fail" stack_text="$stack_text $svc(exited $code)" stack_names="$stack_names $svc" ;;
      running)
        # A one-shot is done when it has exited 0, not while it runs (review m2).
        if [[ "$oneshots" != *" $svc "* ]]; then
          case "$health" in ''|healthy) continue ;; esac
        fi
        [ "$stack" = fail ] || stack="wait"
        stack_text="$stack_text $svc(${health:-running})" stack_names="$stack_names $svc" ;;
      *)
        [ "$stack" = fail ] || stack="wait"
        stack_text="$stack_text $svc($state)" stack_names="$stack_names $svc" ;;
    esac
  done < <(dc ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}')
  return 0
}

restart_counts() {
  local ids
  mapfile -t ids < <(dc ps -aq | sort)
  # The service, from its compose label, whatever the project is called (review m8).
  [ "${#ids[@]}" = 0 ] || docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}} {{.RestartCount}}' "${ids[@]}"
}

# Every service up -- healthy where it has a health check -- and the three
# one-shots exited 0; and still so 15 s later, with no restart in between: a
# service that restarts for ever is up for a moment at a time (row 15 of
# "What was broken"). Those 15 s count towards the 300.
wait_stack() {
  local i before after name
  for ((i = 0; i < 300; i += 5)); do
    stack_state
    if [ "$stack" = fail ]; then
      die "a service stopped:$stack_text" "docker compose logs --tail 50$stack_names" "$rerun"
    fi
    if [ "$stack" = ok ]; then
      before="$(restart_counts)"
      sleep 15
      i=$((i + 15))
      stack_state
      after="$(restart_counts)"
      if [ "$stack" = ok ] && [ "$before" = "$after" ]; then return 0; fi
      if [ "$stack" = ok ]; then
        stack_text="" stack_names=""
        while read -r name _; do
          stack_text="$stack_text $name(restarted)" stack_names="$stack_names $name"
        done < <(diff <(printf '%s\n' "$before") <(printf '%s\n' "$after") | sed -n 's/^> //p')
      fi
    fi
    sleep 5
  done
  die "after 300 s, still waiting on:$stack_text" "docker compose ps -a" "docker compose logs --tail 50$stack_names" "$rerun"
}

step_7() {
  say "Step 7 — start everything; Traefik obtains the certificate for :443"
  local before after mode connect gateway_cert limits tls_args=()
  before="$(dc ps -aq | sort)"
  # Fails when a dependency does: a one-shot that did not exit 0, a service
  # that never turned healthy (review m1).
  dc up -d || die "docker compose up -d failed (above)" "docker compose ps -a" "docker compose logs --tail 50" "$rerun"
  after="$(dc ps -aq | sort)"
  if [ -n "$before" ] && [ "$before" = "$after" ]; then
    skip "step 7: docker compose up -d created no container and recreated none"
  else
    act "docker compose up -d: it started or recreated what was not up"
  fi
  wait_stack
  dc ps -a --format 'table {{.Service}}\t{{.Status}}'
  [ -z "$tls_cafile" ] || tls_args=(--cafile "$tls_cafile")
  ./scripts/check-public-tls.sh "${tls_args[@]}" </dev/null \
    || die "the certificate on :443 is not one a browser accepts (above)" \
           "# the usual causes: the A record of $host is not this machine's, :443 is closed at the provider's firewall, or an AAAA record names another machine; why Let's Encrypt did not issue it is in Traefik's log:" \
           "docker compose logs traefik | grep -i acme" \
           "# fix the cause, then restart Traefik, which asks for the certificate again, and run this again; OpenBao unseals itself, so it asks for no share:" \
           "docker compose restart traefik && $rerun"
  mode="$(dc exec -T traefik stat -c %a /etc/traefik/acme/acme.json 2>/dev/null | tr -d '[:space:]' || true)"
  [ "$mode" = 600 ] || die "Traefik's acme.json is mode ${mode:-missing}, not 600" \
    "# Traefik writes acme.json itself, mode 600, into its traefik-acme volume, and refuses a looser one: another mode, or none, means it was made or changed by something else; its log says what Traefik made of it:" \
    "docker compose logs traefik | grep -i acme" "$rerun"
  ok "step 7: Traefik's certificate store, acme.json, is mode 600"
  connect="$(env_get ITMS_BIND_IP)"
  case "$connect" in ''|0.0.0.0) connect=127.0.0.1 ;; esac
  gateway_cert="$(openssl s_client -connect "$connect:8443" -servername "$host" </dev/null 2>/dev/null \
                  | openssl x509 -noout -issuer -ext subjectAltName 2>/dev/null || true)"
  printf '%s\n' "$gateway_cert" | sed 's/^/        /'
  # OpenSSL 3.0 (Ubuntu 24.04) prints `issuer=CN = Novieri RMM internal CA`, newer
  # ones `issuer=CN=Novieri RMM internal CA`: drop the spaces round `=` and `,`,
  # then the issuer must be that name exactly, not merely contain it.
  gateway_issuer="$(printf '%s\n' "$gateway_cert" | sed -n 's/^issuer=//p' | sed 's/ *= */=/g; s/ *, */,/g')"
  [ "$gateway_issuer" = "CN=Novieri RMM internal CA" ] || gateway_cert=""
  case "$gateway_cert" in
    *"DNS:$host"*) ok "step 7: :8443 presents a certificate for $host whose issuer is named CN=Novieri RMM internal CA: the gateway answers it, not Traefik (the name is compared, not the chain)" ;;
    *) die ":8443 does not present the internal CA's certificate for $host (above), so no agent would connect" \
           "docker compose ps gateway" "docker compose logs --tail 50 gateway traefik" "$rerun" ;;
  esac
  limits="$(dc exec -T api python -c "from app.core.config import get_settings; s = get_settings(); print(s.auth_login_ip_limit, s.auth_factor_ip_limit)" | tr -d '\r')"
  note "step 7: the per-address sign-in limits the api started with: $limits"
}

step_8() {
  say "Step 8 — the install code"
  local needed code
  needed="$(dc exec -T api python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://localhost:8000/api/v1/setup/status', timeout=10))['needed'])" 2>/dev/null | tr -d '[:space:]' || true)"
  case "$needed" in
    False) skip "step 8: the installation is claimed"; claimed=yes ;;
    True)
      code="$(dc exec -T api cat /var/lib/its/secrets/setup-code 2>/dev/null | tr -d '[:space:]' || true)"
      [ -n "$code" ] || die "the api wrote no install code to /var/lib/its/secrets/setup-code (none, or an empty one)" "docker compose logs api | grep -i 'install code'" "$rerun"
      if [ -t 1 ] || [ "$allow_nontty" = 1 ]; then
        printf '\n  install code  %s\n\n' "$code"
        note "open https://$host/ in a browser you trust and enter it (deploy/README.md, First boot, 2): treat it as a password until you have claimed the installation"
      else
        note "the install code is not printed, the output is not a terminal; read it with: docker compose exec api cat /var/lib/its/secrets/setup-code"
      fi
      code="" ;;
    *) die "the api did not answer /api/v1/setup/status" "docker compose logs --tail 50 api" "$rerun" ;;
  esac
}

claimed=no
finish() {
  say "First boot: every step is done."
  if [ "$claimed" = yes ]; then
    note "Next, in a browser (deploy/README.md, First boot, 2): the installation is claimed; sign in at https://$host/"
    note "with your password and second factor; then an enrollment token at https://$host/enrollment."
  else
    note "Next, in a browser (deploy/README.md, First boot, 2): https://$host/ with the install code;"
    note "your password, your second factor and its ten recovery codes; then an enrollment token at https://$host/enrollment."
  fi
  note "Then: ./scripts/first-boot-check.sh"
  note "Custody, off this VPS (GO-LIVE.md §4 and §6): the five shares, RECOVERY keys now: they authorize"
  note "openbao-seal.sh disable, and, with the root token, bao operator generate-root: they are no way back"
  note "from a lost root token; they cannot unseal OpenBao. The root token, whose off-host copy is the only way"
  note "back to root. The key file $key_file, apart from any copy of openbao-data (lose it and OpenBao"
  note "cannot be unsealed). And .env, its-secrets and its-ca."
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
step_4
step_4b
step_5
step_6
step_6b
step_7
step_8
finish

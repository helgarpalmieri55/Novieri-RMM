#!/usr/bin/env bash
# Cross-compile the agent into agent/dist/.
#
#   ./build.sh                 # every target
#   ./build.sh linux/amd64     # just one
#   VERSION=1.4.0 ./build.sh   # stamp it (an unstamped build reports "dev")
#
# One static binary per target, CGO_ENABLED=0, no external toolchain. The same
# six targets the `agent` CI job compiles, so a capability that reaches for a C
# library breaks in CI rather than here.
#
# SIGNING, precisely — three different things that are easy to blur together:
#
#   * darwin/arm64 comes out AD-HOC SIGNED, and needs no step here. Apple
#     Silicon kills an unsigned arm64 Mach-O at exec (a kernel check, not
#     Gatekeeper), and the Go INTERNAL linker — which the CGO_ENABLED=0 below
#     guarantees — embeds an ad-hoc signature itself rather than shelling out
#     to the Mac-only codesign(1). So the binary this produces on Linux runs on
#     Apple Silicon. Asserted, not assumed, by packaging/machosign_test.go,
#     which recomputes every page hash of the artifact THIS SCRIPT builds.
#
#     Two changes would break that silently, and both go red in that test: a
#     capability that actually imports "C" (forcing external linking, which
#     leaves darwin signing to the Mac-only codesign(1)), and any post-link
#     rewrite of the binary (strip, upx, an appended payload), which leaves the
#     load command in place but invalidates the hashes. The first trigger is
#     real cgo USE, not this CGO_ENABLED flag -- flipping the flag alone still
#     yields a signed binary today, because nothing imports "C".
#     Do not add a strip step: -s -w below already omits the symbol table and
#     DWARF at link time, which is the signature-safe way to get the same size.
#
#   * DEVELOPER ID + NOTARIZATION is a separate, still-missing thing, and is
#     about distribution OUTSIDE managed devices. Not needed for the MDM
#     rollout; needed for commercialization.
#
#   * WINDOWS is genuinely unsigned and needs signtool before a real rollout.
#
# See agent/packaging/README.md "Code signing" for the full picture.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
module="$(cd "$here/.." && pwd)"
dist="${DIST:-$module/dist}"

# VERSION defaults to a git describe, then to "dev". main.version is compared by
# operators against what the console reports, so an unstamped binary saying
# "dev" is the honest answer rather than a plausible-looking wrong one.
if [ -z "${VERSION:-}" ]; then
  VERSION="$(git -C "$module" describe --tags --always --dirty 2>/dev/null || echo dev)"
fi

targets=("$@")
if [ ${#targets[@]} -eq 0 ]; then
  targets=(linux/amd64 linux/arm64 windows/amd64 windows/arm64 darwin/amd64 darwin/arm64)
fi

# RELEASE_SIGNING_PUBKEY (base64 Ed25519, from `release-sign pubkey`) is
# compiled in so the agent requires a release signature on every self-update.
# Unset, the build checks the SHA-256 only and says so in its update results.
ldflags="-s -w -X main.version=${VERSION}"
if [ -n "${RELEASE_SIGNING_PUBKEY:-}" ]; then
  ldflags="${ldflags} -X github.com/toeshee/it-suite/agent/internal/update.releaseSigningKey=${RELEASE_SIGNING_PUBKEY}"
fi

mkdir -p "$dist"
for t in "${targets[@]}"; do
  goos="${t%/*}"
  goarch="${t#*/}"
  out="$dist/its-agent-${goos}-${goarch}"
  if [ "$goos" = "windows" ]; then out="${out}.exe"; fi
  printf '== %s\n' "$t"
  (
    cd "$module"
    CGO_ENABLED=0 GOOS="$goos" GOARCH="$goarch" \
      go build -trimpath -ldflags "$ldflags" -o "$out" ./cmd/agent
  )
done

# A convenience copy for the host platform, which is what the install scripts
# look for first.
hostos="$(go env GOOS)"
host="$dist/its-agent-${hostos}-$(go env GOARCH)"
plain="$dist/its-agent"
if [ "$hostos" = "windows" ]; then
  host="${host}.exe"
  plain="${plain}.exe"
fi
if [ -f "$host" ]; then
  cp "$host" "$plain"
fi

printf '\nbuilt %s into %s\n' "$VERSION" "$dist"
ls -l "$dist"

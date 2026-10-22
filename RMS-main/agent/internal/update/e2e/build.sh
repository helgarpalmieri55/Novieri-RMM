#!/usr/bin/env bash
# Build the four agent-update-e2e binaries into $1 (spec-self-update.md §11):
#
#   A  0.0.1-e2e.1
#   B  0.0.1-e2e.2
#   C  0.0.1-e2e.3  -tags e2e_noconfirm   (never dials, so never confirms)
#   D  0.0.1-e2e.4  -tags e2e_badselftest (--update-selftest exits 1)
#
# All four pin a THROWAWAY release-signing key generated here, so the run
# exercises the release-signature check exactly as a release build would. The
# private half is written to $1/release-key.pem for the harness and is never
# the real key: that one exists only as the AGENT_RELEASE_SIGNING_KEY secret.
set -euo pipefail

out="${1:?usage: build.sh OUTDIR}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
module="$(cd "$here/../../.." && pwd)"
mkdir -p "$out"
cd "$module"

key="$(go run ./cmd/release-sign genkey)"
printf '%s\n' "$key" > "$out/release-key.pem"
pub="$(AGENT_RELEASE_SIGNING_KEY="$key" go run ./cmd/release-sign pubkey)"

ext=""
if [ "$(go env GOOS)" = "windows" ]; then ext=".exe"; fi

build() {
  local name="$1" version="$2" tags="$3"
  CGO_ENABLED=0 go build -trimpath -tags "$tags" \
    -ldflags "-s -w -X main.version=${version} -X github.com/toeshee/it-suite/agent/internal/update.releaseSigningKey=${pub}" \
    -o "$out/${name}${ext}" ./cmd/agent
  printf '%s %s %s\n' "$name" "$version" "${tags:-(no tags)}"
}

build A 0.0.1-e2e.1 ""
build B 0.0.1-e2e.2 ""
build C 0.0.1-e2e.3 e2e_noconfirm
build D 0.0.1-e2e.4 e2e_badselftest

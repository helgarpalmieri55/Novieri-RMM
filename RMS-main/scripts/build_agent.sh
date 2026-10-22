#!/usr/bin/env bash
# Build the agent for THIS host, into agent/dist/its-agent.
#
# The one-liner scripts/demo_e2e_agent.py names in its error message. It exists
# so "build the agent" is a command rather than a paragraph, now that the agent
# is in this tree (CLAUDE.md commit `a32cead`) and there is nothing to clone.
#
# For release artifacts — all six platforms, stamped, trimmed — use
# agent/packaging/build.sh instead. This is the developer-loop build: one
# binary, this platform, no version stamp.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root/agent"

mkdir -p dist
# CGO_ENABLED=0 matches every packaged build and every CI cross-compile: a
# binary that links libc here and not there is a difference the demo would hide.
CGO_ENABLED=0 go build -o dist/its-agent ./cmd/agent

echo "built $repo_root/agent/dist/its-agent"
"$repo_root/agent/dist/its-agent" --version

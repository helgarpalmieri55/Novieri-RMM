#!/usr/bin/env bash
# Build the application images and tag them with the current commit.
#
# WHY THIS EXISTS. Until now the five application services built from
# `build:` with no `image:` tag, so there was no artefact to roll back TO --
# `deploy/README.md`'s rollback was `git checkout <old-sha> && rebuild`, and
# a rebuild is not a rollback. Base-image layers, apt/pip/npm resolution and
# build-time downloads can all differ from what originally shipped, so the
# thing you roll forward into after a bad deploy is not the thing that was
# running before it.
#
# With a tag, rollback is retagging: set IT_SUITE_VERSION to the previous
# short SHA and `docker compose up -d`. No rebuild, no network, no drift.
#
# Two image names, not five: api/gateway/worker/migrate build the same
# context with the same Dockerfile and differ only in `command`, so they
# share `itsuite/backend`. `web` is its own artefact.
#
# Usage:
#   ./build-images.sh                 # tag = current short SHA
#   IT_SUITE_VERSION=1.4.2 ./build-images.sh
set -euo pipefail

cd "$(dirname "$0")/.."   # deploy/

if [ -z "${IT_SUITE_VERSION:-}" ]; then
    # REFUSE TO TAG A DIRTY TREE with a bare SHA. A tag that names a commit
    # but contains uncommitted changes is worse than an untagged build: it is
    # a rollback target that cannot be reproduced from the commit it claims.
    if ! git diff --quiet HEAD 2>/dev/null; then
        echo "ERROR: working tree is dirty; a SHA tag would not describe the" >&2
        echo "image's contents. Commit, or set IT_SUITE_VERSION explicitly." >&2
        exit 1
    fi
    IT_SUITE_VERSION="$(git rev-parse --short HEAD)"
fi
export IT_SUITE_VERSION

echo "==> building itsuite/backend:$IT_SUITE_VERSION and itsuite/web:$IT_SUITE_VERSION"
docker compose build api web

echo
echo "built:"
docker images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}' \
    | grep -E "^  itsuite/(backend|web):$IT_SUITE_VERSION\b" || true

cat <<EOF

==> To deploy this version:
      echo "IT_SUITE_VERSION=$IT_SUITE_VERSION" >> .env   # or edit in place
      docker compose up -d

==> To roll back later, set IT_SUITE_VERSION to a PREVIOUS short SHA and
    re-run \`docker compose up -d\`. Recent tags:
$(docker images --format '      {{.Repository}}:{{.Tag}}' 2>/dev/null | grep '^      itsuite/backend:' | head -5 || echo "      (none yet)")
EOF

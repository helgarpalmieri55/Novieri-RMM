# MinIO, built here from its public source, because nobody publishes it any more.
#
# WHY THIS FILE EXISTS (2026-09-24). MinIO stopped distributing free images and
# binaries: `minio/minio` on Docker Hub was deleted, `quay.io/minio/minio`
# answers 401 to anonymous pulls, and `dl.min.io`'s release archive answers
# 410. Every fresh install of this platform, and CI, failed at the first pull.
# The operator chose to build from source rather than trust a third party's
# rebuild of the service that holds every backup, artefact and recording.
#
# PINNED TWICE. A tag names the release; the commit hash is what is actually
# compiled, and the build refuses to continue if the tag no longer points at
# it. Tags can be moved; a hash cannot be made to mean different source.
#
# THE SAME MINIO RELEASE THE PLATFORM ALREADY RAN (2025-09-07), so the data
# under `minio-data` is read by the version that wrote it. Moving to a newer
# release is a separate, deliberate change to the two MINIO_* arguments.
#
# `mc` IS INCLUDED because the old image carried it and three things use it:
# the compose healthcheck (`mc ready local`) and the platform backup scripts
# (`mc mirror`, deploy/scripts/backup.sh and scheduled-backup.sh).

ARG GO_VERSION=1.24

# Debian-based, not -alpine: it already carries git and CA certificates, so
# the build installs no packages at all.
FROM golang:${GO_VERSION}-bookworm AS build

ARG MINIO_RELEASE=RELEASE.2025-09-07T16-13-09Z
ARG MINIO_COMMIT=07c3a429bfed433e49018cb0f78a52145d4bedeb
ARG MC_RELEASE=RELEASE.2025-08-13T08-35-41Z
ARG MC_COMMIT=7394ce0dd2a80935aded936b09fa12cbb3cb8096

ENV CGO_ENABLED=0 \
    GOFLAGS=-trimpath

WORKDIR /src

# `gen-ldflags.go` stamps the release name and commit into the binary, which is
# what `minio --version` reports; it reads them from the checkout, so the clone
# keeps enough history for `git describe` of the tag.
RUN git clone --branch "${MINIO_RELEASE}" --depth 1 https://github.com/minio/minio minio \
 && cd minio \
 && test "$(git rev-parse HEAD)" = "${MINIO_COMMIT}" \
      || { echo "minio ${MINIO_RELEASE} is not commit ${MINIO_COMMIT}: refusing to build" >&2; exit 1; } \
 && go build -ldflags "$(go run buildscripts/gen-ldflags.go) -s -w" -o /out/minio .

RUN git clone --branch "${MC_RELEASE}" --depth 1 https://github.com/minio/mc mc \
 && cd mc \
 && test "$(git rev-parse HEAD)" = "${MC_COMMIT}" \
      || { echo "mc ${MC_RELEASE} is not commit ${MC_COMMIT}: refusing to build" >&2; exit 1; } \
 && go build -ldflags "$(go run buildscripts/gen-ldflags.go) -s -w" -o /out/mc .

FROM alpine:3.21

ARG MINIO_RELEASE=RELEASE.2025-09-07T16-13-09Z
LABEL org.opencontainers.image.title="minio (built from source for ITMS)" \
      org.opencontainers.image.version="${MINIO_RELEASE}" \
      org.opencontainers.image.source="https://github.com/minio/minio" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

# CA certificates from the build stage rather than `apk add`: the runtime stage
# installs nothing, so it has nothing to fetch and nothing that can drift.
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/minio /out/mc /usr/bin/

# The upstream image's layout, so nothing that ran it has to change: data at
# /data, `minio` as the entrypoint, `--entrypoint mc` for the client.
ENV MC_CONFIG_DIR=/tmp/.mc
EXPOSE 9000 9001
ENTRYPOINT ["minio"]

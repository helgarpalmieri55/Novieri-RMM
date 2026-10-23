ui = false

# NOTE: do NOT add `disable_mlock` here. OpenBao dropped mlock support in
# v2.x and *rejects the key outright* -- `error loading configuration from
# /bao/config.hcl: OpenBao has dropped support for mlock` and the container
# exits 1 before it ever listens. This file carried `disable_mlock = false`
# until the first-boot runbook was actually executed, which is why step 3
# had never worked. Protect the secret store from swap at the host level
# instead (no swap, or an encrypted swap device) -- see deploy/GO-LIVE.md.

# /openbao/file, NOT a path of our own choosing. It is the directory the
# image already creates owned by uid 100 (`openbao`) and declares as a
# VOLUME. A named volume mounted anywhere else -- this file said /bao/data
# -- is created root-owned by Docker, and the unprivileged server process
# then fails `bao operator init` with
#   failed to persist keyring: mkdir /bao/data/core: permission denied
# which is the second reason first-boot step 3 had never completed.
storage "file" {
  path = "/openbao/file"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true   # in-cluster only; Traefik never exposes this port
}

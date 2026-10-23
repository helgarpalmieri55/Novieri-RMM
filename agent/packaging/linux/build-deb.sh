#!/usr/bin/env bash
# Build its-agent_<version>_<arch>.deb — the primary Ubuntu artifact.
#
#   ./build-deb.sh                       # amd64, version from git describe
#   ARCH=arm64 VERSION=1.4.0 ./build-deb.sh
#
# Why a .deb and not a tarball plus a shell script: an Ubuntu operator expects
# `apt install ./its-agent_x.y.z_amd64.deb`, and dpkg's maintainer scripts make
# install, upgrade and removal transactional in a way a hand-rolled installer
# never quite is — the service is stopped before its binary is replaced, the
# unit is disabled before it is deleted, and `apt purge` is a real, defined
# operation rather than a flag someone remembered to pass.
#
# THE PACKAGE CARRIES NO CREDENTIALS. Enrollment is a separate, post-install
# step (`its-agent-enroll`), because a per-org token inside a .deb would be a
# credential in a distributable artifact — see invariant #3 and the header of
# its-agent-enroll.
#
# Requires: dpkg-deb (dpkg-dev on Ubuntu). Produces a package that depends on
# nothing: the binary is CGO_ENABLED=0 and statically linked, so there is no
# libc floor and one package runs on Ubuntu 24.04 and later.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
module="$(cd "$here/../.." && pwd)"
dist="${DIST:-$module/dist}"

ARCH="${ARCH:-amd64}"
BINARY="${BINARY:-}"

if [ -z "${VERSION:-}" ]; then
  VERSION="$(git -C "$module" describe --tags --always --dirty 2>/dev/null || echo 0.0.0)"
fi
# dpkg version fields may not contain a leading 'v' or a '/'. `git describe`
# happily produces both.
VERSION="${VERSION#v}"
VERSION="${VERSION//\//-}"

command -v dpkg-deb >/dev/null 2>&1 || {
  printf 'build-deb.sh: dpkg-deb not found (apt install dpkg-dev)\n' >&2; exit 1; }

# --- the binary ------------------------------------------------------------
if [ -z "$BINARY" ]; then
  # ALWAYS rebuilt, never reused from dist/. A binary left there by an earlier
  # run carries that run's -X main.version, and a package whose version and
  # whose `its-agent --version` disagree is an unanswerable support question.
  # (Observed: dist/ held a `git describe` build, and its-agent_0.1.0.deb
  # shipped a binary reporting "2baebb0-dirty".)
  printf '== building linux/%s at version %s\n' "$ARCH" "$VERSION"
  VERSION="$VERSION" "$here/../build.sh" "linux/${ARCH}" >/dev/null
  BINARY="$dist/its-agent-linux-${ARCH}"
fi
[ -f "$BINARY" ] || { printf 'build-deb.sh: no binary at %s\n' "$BINARY" >&2; exit 1; }

# A dynamically linked binary would reintroduce the glibc version floor the
# static build exists to avoid: built on 26.04, installed on 24.04, fails at
# exec with a GLIBC_2.4x error. Checked rather than assumed.
if command -v file >/dev/null 2>&1; then
  case "$(file -b "$BINARY")" in
    *"statically linked"*) ;;
    *) printf 'build-deb.sh: %s is not statically linked; rebuild with CGO_ENABLED=0\n' "$BINARY" >&2; exit 1 ;;
  esac
fi

# --- staging tree ----------------------------------------------------------
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT
# mktemp -d gives 0700, and the staging root IS `/` in the package: shipping it
# that way makes dpkg apply 0700 to the filesystem root on install. Caught by
# reading `dpkg-deb --contents` output, which is why that command is the last
# line of this script.
chmod 0755 "$root"

install -d -m 0755 "$root/DEBIAN"
install -d -m 0755 "$root/usr/bin"
install -d -m 0755 "$root/usr/sbin"
# /usr/lib, not /lib: Ubuntu 24.04 is usr-merged and base-files DIVERTS /lib,
# so a package shipping /lib/systemd/system installs through a diversion and
# `dpkg -L` shows it. /usr/lib/systemd/system is the correct modern location and
# systemd has read it for years.
install -d -m 0755 "$root/usr/lib/systemd/system"
install -d -m 0755 "$root/usr/share/doc/its-agent"

install -m 0755 "$BINARY" "$root/usr/bin/its-agent"
install -m 0755 "$here/its-agent-enroll" "$root/usr/sbin/its-agent-enroll"

# One unit file in the repository, two install paths. The shell installer puts
# the binary in /usr/local/bin (where a hand-installed binary belongs); a
# package must not write to /usr/local, so the packaged unit points at /usr/bin.
sed 's|^ExecStart=/usr/local/bin/its-agent$|ExecStart=/usr/bin/its-agent|' \
  "$here/its-agent.service" > "$root/usr/lib/systemd/system/its-agent.service"
chmod 0644 "$root/usr/lib/systemd/system/its-agent.service"
grep -q '^ExecStart=/usr/bin/its-agent$' "$root/usr/lib/systemd/system/its-agent.service" || {
  printf 'build-deb.sh: the ExecStart rewrite did not apply; refusing to ship a unit that points nowhere\n' >&2
  exit 1
}

install -m 0644 "$here/../README.md" "$root/usr/share/doc/its-agent/README.md"

for script in postinst prerm postrm; do
  install -m 0755 "$here/debian/$script" "$root/DEBIAN/$script"
done

size="$(du -sk "$root" | cut -f1)"
sed -e "s|@VERSION@|${VERSION}|" -e "s|@ARCH@|${ARCH}|" -e "s|@SIZE@|${size}|" \
  "$here/debian/control.in" > "$root/DEBIAN/control"
chmod 0644 "$root/DEBIAN/control"

# The state and config directories are created by postinst rather than shipped,
# so their modes are set in one place and dpkg never records 0700 directories it
# would then try to reconcile on upgrade.

# --- build -----------------------------------------------------------------
mkdir -p "$dist"
out="$dist/its-agent_${VERSION}_${ARCH}.deb"
# --root-owner-group: build without fakeroot and still record root:root, so the
# package is identical whether or not the builder is root.
dpkg-deb --root-owner-group --build "$root" "$out" >/dev/null
printf '%s\n' "$out"
dpkg-deb --contents "$out"

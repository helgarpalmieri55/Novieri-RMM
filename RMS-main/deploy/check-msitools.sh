#!/bin/sh
# Refuses to let the backend image build without a wixl that can make the
# Group Policy MSI (deploy/backend.Dockerfile runs this at build time).
#
# wixl older than 0.102 is refused: the MSI's ARM64 build and the table
# layout app/services/msi_build.py reads back were written against 0.102 and
# later. `wixl --version` prints the bare version ("0.106"); anything that
# does not parse as MAJOR.MINOR is a refusal, never a guess.
set -eu

for tool in wixl msibuild msiinfo; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "check-msitools: $tool is not on PATH" >&2
        exit 1
    fi
done

raw=$(wixl --version 2>/dev/null | head -n 1 | tr -d '[:space:]') || raw=""
case "$raw" in
    [0-9]*.[0-9]*) ;;
    *)
        echo "check-msitools: cannot read a version from 'wixl --version' ('$raw')" >&2
        exit 1
        ;;
esac
major=${raw%%.*}
rest=${raw#*.}
minor=${rest%%[!0-9]*}
case "$major$minor" in
    *[!0-9]*|"")
        echo "check-msitools: cannot read a version from 'wixl --version' ('$raw')" >&2
        exit 1
        ;;
esac
if [ "$major" -eq 0 ] && [ "$minor" -lt 102 ]; then
    echo "check-msitools: wixl $raw is older than 0.102; the MSI needs 0.102 or later" >&2
    exit 1
fi
echo "check-msitools: wixl $raw, msibuild, msiinfo"

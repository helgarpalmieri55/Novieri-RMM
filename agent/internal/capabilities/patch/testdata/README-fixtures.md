# What these fixtures are, and which of them were captured

Read this before trusting a test in this package. The two files here have very
different standing and it is not visible from their names alone, which is why
the reconstructed one carries `RECONSTRUCTED` in its filename.

## `apt-get-s-upgrade-real.txt` — CAPTURED

`apt-get -s -o Debug::NoLocking=1 upgrade`, run verbatim on the Ubuntu 24.04
container this package was written in, with only the shell's own `exit=` line
removed. 372 lines, 166 `Inst` lines, 136 of them naming a `-security` pocket.

It earned its place: it contains two shapes the parser's author did not know
about until the capture showed them — comma-separated multi-origin groups
(`Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security`) and a trailing `[]`
group after the closing parenthesis. An invented fixture would have had exactly
the shape the parser already handled, and the tests over it would have measured
nothing.

## `softwareupdate-list-RECONSTRUCTED.txt` — NOT CAPTURED

Written by hand from the documented format of `softwareupdate --list`. **No Mac
was involved.** Nobody working on this package has run `softwareupdate`, and
this repository's CI cannot.

What that means for the tests that read it: they establish that
`parseSoftwareUpdateList` handles the format *as this file states it* —
label/attribute pairing, the `Recommended` → severity mapping, the `Action:
restart` flag, and a trailing label with no attribute line. They establish
NOTHING about whether real `softwareupdate` output has that format on any macOS
release. If it does not, these tests stay green and every Mac in the estate
reports as fully patched, which is the most dangerous wrong answer this
capability can give.

The first person with a Mac should run

    /usr/sbin/softwareupdate --list

capture both stdout and stderr, replace this file with the capture, rename it
to `softwareupdate-list-real.txt`, and fix whatever the parser then gets wrong.
See `docs/modules/patch/agent-capabilities.md` §6.

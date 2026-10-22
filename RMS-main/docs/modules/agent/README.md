# The agent — where it is, how to run it, and what is still open

The endpoint half of the platform. One static Go binary per endpoint: one
long-lived **outbound** mTLS WebSocket, one timer wheel, and a registry of
capabilities that are dormant until a signed job for them arrives.

**It lives in this repository, under `agent/`.** It used to live in a separate
`it-suite-agent` repository; `CLAUDE.md` commit `a32cead` reversed that on
2026-08-05. If you find a doc or a docstring that still says otherwise, it is
stale — see [What still says the agent is elsewhere](#what-still-says-the-agent-is-elsewhere).

| | |
|---|---|
| Code | `agent/` (its own Go module, `github.com/toeshee/it-suite/agent`) |
| Reference | `agent/README.md` (module-level), `agent/packaging/README.md` (endpoints) |
| Spec | `docs/05-agent-spec.md` |
| Plan | `docs/modules/agent/plan.md` |
| Wire contract | `docs/kernel/wire-protocol/` |
| CI | the `agent` job in `.github/workflows/ci.yml` |

The agent does not import the backend and the backend does not import it. The
only thing that crosses is `docs/kernel/wire-protocol/*.schema.json`.

---

## Build

```sh
scripts/build_agent.sh          # -> agent/dist/its-agent, this platform
```

For release artifacts — six platforms, stamped, trimmed, statically linked:

```sh
cd agent && VERSION=1.4.0 ./packaging/build.sh
```

Targets: `linux/{amd64,arm64}`, `windows/{amd64,arm64}`, `darwin/{amd64,arm64}`.
Every one is `CGO_ENABLED=0`. See `agent/packaging/README.md` for why the static
link is load-bearing (one package covers Ubuntu 24.04 and 26.04).

## Test

```sh
cd agent
go test ./... -race -count=1     # the suite
go vet ./...
gofmt -l .                       # must print nothing
GOOS=windows go vet ./...        # the windows-only files compile for nobody else
```

`-count=1` defeats the test result cache: a cached PASS from a previous run is
not evidence about this commit.

**A green backend suite says nothing about the agent**, which is why the `agent`
CI job exists separately. It runs gofmt, vet, staticcheck, the tests, the idle
budget, six cross-compiles, a `GOOS=windows` vet and test-compile, shellcheck
over the packaging scripts, `systemd-analyze verify`, and a full
install→enroll→remove→purge `.deb` lifecycle.

## Run and enroll

On a development box, against a control plane you are running locally:

```sh
ITS_AGENT_DATA_DIR=/tmp/its-agent-data \
ITS_AGENT_ENROLL_URL=https://localhost:8443 \
ITS_AGENT_ENROLL_TOKEN=<token> \
  agent/dist/its-agent
```

On a real endpoint, use the packaging — it is the path that handles the
enrollment token correctly:

```sh
sudo apt install ./its-agent_0.1.0_amd64.deb
sudo ITS_AGENT_ENROLL_TOKEN=<token> its-agent-enroll --url https://its.example.com
```

**The token never goes in argv.** Every installer here refuses `--token` and
tells you what to use instead: `/proc/<pid>/cmdline` is world-readable for the
life of the process and your shell keeps it in history. It goes into one
`0600` root-owned file in the data directory, which the agent reads once and
deletes the moment it has exchanged it for a certificate.
`agent/packaging/README.md` has the full argument.

## Prove the whole path works

```sh
scripts/build_agent.sh
backend/.venv/bin/python scripts/demo_e2e_agent.py
```

Boots the API and the mTLS gateway on real ports, enrolls the real Go binary,
and dispatches server-signed jobs the agent verifies in Go before executing.
It is the only thing in the repository that proves the Python signer and the Go
verifier agree: `pytest` exercises the signer against a Python reader, and
`go test` exercises the verifier against Go-built fixtures. Neither can catch a
disagreement between them.

Requires local Postgres 16 and Redis on `localhost:6379`.

---

## Capabilities this binary actually has

The registry is one list in `agent/internal/capabilities/registry.go`, and
`capabilities.Names()` — not a hand-written list — is what the agent advertises
at enrollment.

| Job type | Package | Notes |
|---|---|---|
| `hello` | `capabilities/hello.go` | the kernel smoke job |
| `inventory.collect` | `capabilities/inventory/` | hardware/software/NIC facts |
| `agent.cert.renew` / `agent.cert.install` | `capabilities/cert/` | `docs/modules/agent/cert-renewal.md` |
| `health.policy.apply` / `health.check.now` | `capabilities/health/` | the edge tick; inert until a policy arrives |
| `net.sweep` | `capabilities/netsweep/` | job-scoped ICMP/ARP; cannot widen its own signed scope |
| `site.wake` | `capabilities/site/` | Wake-on-LAN from a promoted site server |

Every exported schema in `docs/kernel/wire-protocol/` that has **no** handler
here is named, with a reason, in `agent/internal/wire/schema_test.go`'s
`notImplemented` map. A new schema file that appears in neither map fails that
test — so an unbuilt contract is "not done yet" rather than "forgotten".

## The idle budget, and what is actually measured

`CLAUDE.md`: *"Measured, not asserted."* The numbers below come from tests that
**fail the build**, not from benchmarks that print and pass.

That distinction is the whole mechanism. `go test -bench` **exits 0 on a
regression** — measured on this project, not assumed: a planted 64 KiB/op
regression printed `65536 B/op` and still exited 0 (`agent/internal/budget`'s
package comment). So every budget number is produced by `testing.Benchmark`
called from an **ordinary `Test`** that then compares against a threshold.

| Subject | Where | Falsifiers |
|---|---|---|
| The timer wheel | `internal/budget/budget_test.go` | zero allocations per wakeup; no goroutine per entry; an idle wheel does not wake |
| `health.check` | `internal/capabilities/health/budget_test.go` | §0.5's six, one test each, named for it |
| `site.wake` | `internal/capabilities/site/budget_test.go` | §5.4's two: nothing resident with no wake in flight; RSS back to baseline after one |

**`~0% CPU at idle` is deliberately NOT asserted as wall-clock**, and that is a
refusal rather than an oversight. A shared runner's variance exceeds the signal:
a threshold loose enough not to flake is loose enough to pass a real regression,
and a flaky budget test gets skipped — which reads as coverage while measuring
nothing. It stays a design target and is described that way, approached through
machine-independent proxies (allocation counts, goroutine counts, file
descriptors, retained heap across a hundredfold spread).

The `agent` CI job prints all of these by name, in bytes, in a dedicated step.
A budget nobody can read the value of drifts back into being a target.

---

## Open questions

Three, each with the condition that would close it.
Full entries: [`open-questions.md`](open-questions.md).

### OQ-1 — certificate renewal — **CLOSED**

Closed by [`cert-renewal.md`](cert-renewal.md): renewal is a signed job
(option (b)), certificates are 365 days, rotation at two-thirds of life,
re-attempted on every connect, and the swap is a transaction with a recovery
marker. The original write-up is kept because the three candidate designs are
the review this platform's identity path is owed.

### OQ-2 — the legacy `attributes.ip` key — **CLOSED**

Closed 2026-08-05: removed from both sides, because the product owner confirmed
nothing is deployed anywhere, which satisfied both removal conditions vacuously.
The argument is preserved because *"if a compatibility key is ever needed again,
it is a wire-protocol decision, not a line added back to either file."*

### OQ-3 — no certificate-backed signature, and therefore no self-update — **OPEN**

**Blocking condition: nothing here can close it.** Code signing needs an EV/OV
Windows certificate, an Apple Developer ID and notary key, a decision between a
signed `.deb` and a signed apt repository, and a release workflow separate from
the per-PR build. Three of the four are procurement; the fourth is a
supply-chain boundary (a signing step on a fork PR hands the key to whoever
opened it).

**Narrowed 2026-08-07: macOS ad-hoc signing is done and is not part of this.**
Apple Silicon's kernel refuses to `exec` an arm64 Mach-O with *no* signature —
an absolute no MDM profile or Gatekeeper setting can waive. The Go internal
linker (which `CGO_ENABLED=0` selects) already embeds an ad-hoc signature when
cross-compiling from Linux, so that requirement is satisfied, and
`agent/packaging/machosign_test.go` now asserts it page hash by page hash in
CI rather than leaving it to a toolchain default nobody chose. What OQ-3 still
covers on macOS is **Developer ID + notarization**, which matters for
distribution outside managed devices.

**Self-update no longer follows from this (decided 2026-09-23).** The
operator approved it as a signed `agent.update` job pinned to the release
SHA-256, with local rollback and rings:
`docs/modules/agent-distribution/spec-self-update.md`. The next two paragraphs
are the earlier position, kept as the record. OQ-3 remains open for signing
itself.

*Earlier position:* self-update is a **consequence**, not a separate item:
`docs/05-agent-spec.md`'s sequence has "verifies the code signature and hash"
as step 3, and there is no signing chain to verify against.

**Do not build the shortcut.** Downloading a binary over the already-mTLS
channel and checking a control-plane-supplied hash is not equivalent to
verifying a signature: it makes a compromised control plane able to replace
root/SYSTEM code on every endpoint while vouching for itself. That is the same
argument invariant #1 already makes about jobs — "the transport is
authenticated" was not accepted as sufficient there either.

Consequence today: **Windows is the genuinely unsigned platform** — SmartScreen
and EDR treat an unsigned SYSTEM service as exactly what it looks like. macOS
is **deployable via MDM and unexecuted by anybody**, which is a different and
much weaker claim than the "undeployable" this section carried until
2026-08-07; that sentence attributed to Gatekeeper an absolute only the
kernel's ad-hoc check has, and Gatekeeper assesses *quarantined downloads*, not
MDM-delivered payloads. The Debian path is unaffected and is the tested one.

---

## What still says the agent is elsewhere

`docs/` is clean: every remaining mention of `it-suite-agent` in a doc is
correctly past-tense, naming `CLAUDE.md` commit `a32cead` as the reversal.

**Thirteen backend docstrings are not**, and they are listed here rather than
left to be rediscovered. Each describes the schema export as a hand-off to a
separate repository, which stopped being true on 2026-08-05:

```
backend/app/gateway/protocol.py
backend/app/modules/{deploy,ipam,inventory,backup,patch,provisioning}/schema.py
backend/app/modules/{reporting,ai_admin,health}/__init__.py
backend/app/modules/health/{registry,jobtypes}.py
backend/tests/test_health_wire.py
```

They are comments, so nothing behaves incorrectly because of them. What they
cost is the next reader's time, and one specific wrong conclusion: *"the Go
capability for this lives in another repo, so it is not my problem."* It is in
`agent/` and it is.

**The export is still a compatibility contract**, and that is the thing those
docstrings get right for the wrong reason. One repository makes it *possible* to
change both sides in one commit and *tempting* to treat the envelope as an
internal detail. A fleet is always mixed-version, so every change to
`docs/kernel/wire-protocol/` is additive, or versioned, or it breaks agents in
the field — and no test in this repository will tell you which.

# its-agent

The Toeshee endpoint agent. One static Go binary per endpoint: one long-lived
outbound mTLS WebSocket, one timer wheel, and a registry of capabilities that
are dormant until a signed job for them arrives.

It is its own Go module (`github.com/toeshee/it-suite/agent`). It does not
import the backend and the backend does not import it. It lives in this
repository as of `CLAUDE.md` commit `a32cead`; it used to be a separate
`it-suite-agent` repo.

## Build and test

```sh
cd agent
go build ./...
go vet ./...
go test ./...
```

Cross-compilation targets (all `CGO_ENABLED=0`, no cgo anywhere):
`windows/amd64`, `windows/arm64`, `linux/amd64`, `linux/arm64`,
`darwin/amd64`, `darwin/arm64`.

CI runs all of the above in the `agent` job of `.github/workflows/ci.yml`,
plus `gofmt -l` and `staticcheck`. **A green backend suite says nothing about
this module**, which is why that job exists separately.

## Putting it on a real endpoint

`packaging/` holds the service definitions and installers — a Debian package
for Ubuntu (the tested path), a shell installer for any systemd host, a launchd
plist, and a Windows service. Start at
[`packaging/README.md`](packaging/README.md); the short version for Ubuntu is:

```sh
VERSION=0.1.0 ./packaging/linux/build-deb.sh
sudo apt install ./dist/its-agent_0.1.0_amd64.deb
sudo ITS_AGENT_ENROLL_TOKEN=<token> its-agent-enroll --url https://its.example.com
```

The enrollment token is a credential and is treated as one: it never appears in
argv, in a unit file, or inside the package. See that README's first section.

## Configuration

Three environment variables, whose names are fixed by
`scripts/demo_e2e_agent.py`, plus one file:

| Variable | Meaning |
|---|---|
| `ITS_AGENT_ENROLL_URL` | API base URL. Enrollment posts to `<url>/enroll/v1` — the enrollment router is mounted with **no** prefix, so it is not under `/api/v1`. |
| `ITS_AGENT_ENROLL_TOKEN` | Single-use enrollment token. Needed only on a first run. |
| `ITS_AGENT_DATA_DIR` | Identity, key, pins and replay store. Defaults to `/var/lib/its-agent`, or `%ProgramData%\ITSuite\agent` on Windows. |

The token may instead arrive as a **`0600` file** at
`<data dir>/enroll-token`, which is how every installer under `packaging/`
hands it over: argv is world-readable through `/proc/<pid>/cmdline`, and a
`systemd` `Environment=` line is printed by `systemctl show` and outlives the
token. The environment wins if both are set; the agent refuses to start if the
file is group- or world-readable, and **deletes it** the moment it has exchanged
it for a certificate.

An already-enrolled agent needs neither URL nor token: the token is consumed at
first boot and cannot be replayed, so requiring one per restart would make a
reboot un-survivable. A *spent* token file with a bad mode is therefore not
fatal — it is only consulted before enrollment, and is deleted on the next boot.

Nothing that affects job verification is configurable. The pinned CA bundle,
the pinned job-signing key and the gateway URL all arrive from enrollment and
are persisted under the data dir, so no environment value can weaken the checks.

## The invariants, as they apply here

1. **No unsigned command path.** Not for tests, not behind a build tag. The
   verified-job type is unconstructable outside `internal/jobs`, and every
   security test asserts *that the capability did not run*, not merely that an
   error came back.
2. **Outbound only.** `net.Listen*` and `http.Server` never appear in non-test
   agent code; an AST walker and a `/proc/net` diff enforce it.
3. **The device private key** is written `0600`, never logged, never sent.
4. **Audit is the server's.** The agent writes a local log and produces no
   audit records.
5. **/6. Tenancy and RBAC are the server's.** The agent asserts no `org_id` and
   no permission; its identity is its certificate.

## The wire protocol is a compatibility contract

`docs/kernel/wire-protocol/*.schema.json` — exported from the Python models —
is what the Go structs in `internal/wire` are built against, not somebody's
reading of `protocol.py`. A deployed agent is always older than the control
plane it talks to: **additive, or versioned, or it is a break**, and *loosen
inbound before you tighten outbound*. Living in one repository makes that
easier to violate, not safer. See `docs/kernel/wire-protocol/README.md`.

## Dependencies

Two third-party modules are budgeted for the whole agent. One is in use:

| Module | Why |
|---|---|
| `github.com/coder/websocket` | The WebSocket client. Stdlib-only itself, no cgo, and it exposes close codes — which the transport needs, because 4401/4403 are terminal verdicts and everything else is a retry. |
| `golang.org/x/net` (`icmp`, `ipv4`, `ipv6`) | Not yet added. Task 13 (`net.sweep`) only. |

Anything else needs a written justification in the task that adds it.

## Transport

`internal/transport` is the one connection: outbound, mutual TLS 1.3,
`wss://` only. It refuses a `ws://` or `http://` gateway URL and refuses to
dial without a pinned CA pool, because "pinned" has to be a fact rather than a
claim.

While connected it holds **two goroutines**: one parked in a blocking
websocket read, one parked on a channel receive waiting to write (a single
owned writer, because `coder/websocket` forbids concurrent writers). Nothing
polls — the idle test asserts **zero socket reads return** across a 200 ms idle
window, which is the observable difference between a blocking read and a poll
loop. Everything time-based goes through an injectable `Clock`, so backoff and
heartbeat are asserted on observed durations instead of by sleeping.

Reconnect is `min(Max, Base<<attempt)` jittered into `[0.5x, 1.5x]`, and the
attempt counter resets only after a connection has *lived* for `ResetAfter` —
resetting on a bare successful dial would mean a gateway that accepts and
instantly drops gets redialled every `Base` forever. Close codes **4401** and
**4403** are terminal: a revoked agent that reconnected forever would be a
denial-of-service against its own control plane.

The heartbeat ticks every 60 s and is not decoration:
`agents.last_seen_at` used to be written only at connect and disconnect, so a
healthy agent aged past `offline_after_s` (default 900 s) and got a false
`agent.offline` alert with its socket open. The gateway now refreshes that
column from a heartbeat (rate-limited to one write per 30 s per device, with no
audit record and the *server's* timestamp, not the agent's).

## Enrollment, identity and pinning

`internal/enroll` generates an Ed25519 keypair, POSTs a CSR to
`<ITS_AGENT_ENROLL_URL>/enroll/v1`, and persists three files under the data
dir (mode `0700`): `identity.json`, `agent.key` (mode `0600`), `pins.json`.

`identity.json` is renamed into place **last**, so it is the commit marker: an
interrupted enrollment leaves a key and pins that the next boot ignores and
overwrites, never a half-identity that is treated as real. Nothing is written
at all until the response has been fully validated — the leaf parses, its
public key is the one just generated, the CA bundle yields a usable pool, and
the job-signing pin is Ed25519. A rejected enrollment leaves the directory
empty; a partial identity is worse than none, because the next boot would find
it, skip enrollment, and fail mTLS forever with a token already spent.

An already-enrolled data directory is **never** re-enrolled, and a corrupt one
is an error rather than a silent re-enrollment: tokens are use-capped, so an
agent that re-enrolls per boot burns the fleet's tokens and mints a duplicate
`Device` row each time.

`capabilities` is advertised at enrollment as a strictly additive, optional
field. It comes from the capability registry, never from a hand-written list —
today the registry is empty, so the agent advertises `{}`, which is what the
column already defaults to.

## The certificate is a 30-day fuse

The CA issues agent certificates with `ttl_days=30` and **there is no renewal
path anywhere in the platform**. `internal/enroll/lifecycle.go` makes the agent
*aware* of that: `ExpiryState` reports `ok` / `expiring` / `expired` from the
certificate's own `NotBefore`/`NotAfter` (so the threshold scales if `ttl_days`
changes), and `LogExpiry` writes one WARN per connect from day 20 and one ERROR
once expired, naming the exact `NotAfter`. It allocates nothing, so it is free
to call on every connect.

It does **not** renew, and it must not: that would be a new authenticated path
into the fleet's identity system. See `docs/modules/agent/open-questions.md`
**OQ-1** for the facts, three candidate designs and the recommendation.

## Canonical JSON

`internal/canonical` is a byte-exact port of `backend/app/jobs/canonical.py`.
Go's `encoding/json` cannot produce those bytes (it HTML-escapes `<>&`, emits
``/`` instead of `\b`/`\f`, escapes U+2028/U+2029, and reformats
numbers through `float64`), so the encoder is hand-rolled and pinned to
`internal/canonical/testdata/corpus.jsonl`, which is **generated by the Python
signer** via `scripts/export_canonical_corpus.py`. If those bytes diverge, the
fleet refuses every job it is sent while every test in this repo stays green —
hence the corpus rather than an argument.

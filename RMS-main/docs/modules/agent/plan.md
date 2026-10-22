# Agent (in-tree Go agent) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Go agent under `agent/` in this repository, from nothing, up to the point where the three capabilities that shipped control-plane-only — `health.check` (Phase 16), `net.sweep` (Phase 15), and the inventory NIC address block (IPAM task 13) — have an endpoint that answers them, and the idle budget those specs claim is measured by a benchmark that fails CI.

**Architecture:** One static binary. One long-lived outbound mTLS WebSocket parked on a blocking read, one timer wheel, and a capability registry of compiled-in packages that are dormant until a signed job for them arrives. Nothing executes that has not passed the signed-job runner: signature over canonical bytes against the pinned Ed25519 key, target-device match, freshness, nonce non-replay, known job type — in that order, with the verified job represented by a type no other package can construct.

**Tech Stack:** Go 1.24 (the toolchain in this container is `go1.24.7`). Standard library for TLS 1.3, `crypto/ed25519`, JSON, and timers. Exactly two third-party modules: `github.com/coder/websocket` (stdlib-only WebSocket client) and `golang.org/x/net` (`icmp`, `ipv4`, `ipv6`) for Task 13. No cgo. No other dependency without a written justification in the task that adds it.

---

## Global Constraints

Every task's requirements implicitly include this section.

### Authority, and what to do when this plan is wrong

- **Authority order:** the code, then `docs/kernel/wire-protocol/` (the exported schemas), then `CLAUDE.md`, then the module specs, then this plan.
- **Trust reality over this plan.** This plan's author read the code but ran almost none of it. If a task's brief contradicts what you are looking at, the code wins: say so in your report, record the deviation, and do not implement a brief you have just proven wrong.
- The wire protocol's compatibility rules are in `docs/kernel/wire-protocol/README.md` and are binding: **additive, or versioned, or it is a break**; **loosen inbound before you tighten outbound**; **the test suite will not catch this.**

### Verified facts you will build against

Checked against the tree at plan time. Re-check anything you depend on.

| Fact | Where |
|---|---|
| There is **no Go code and no `agent/` directory** in this repo | `find . -name '*.go'` returns nothing |
| Enrollment endpoint is `POST /enroll/v1` — **not** under `/api/v1` | `app/main.py:65` (`include_router(enroll_router)` with no prefix), `app/api/v1/enrollment.py:144` |
| `EnrollRequest` = `{token, csr_pem, device_facts{hostname, os_family, os_version, architecture, serial}}`; rejection is a bare **403** | `app/api/v1/enrollment.py:40-52,183` |
| `EnrollResponse` = `{device_id, cert_pem, chain_pem, ca_bundle_pem, gateway_url, job_signing_public_key_pem}` — **six fields, no policy** | `app/api/v1/enrollment.py:44-51` |
| Client certs are issued for **30 days** (`sign_csr(..., ttl_days=30)`), CN = `device_id`, O = `org_id`, SAN URI `its://org/<org_id>/device/<device_id>`, EKU clientAuth | `app/core/ca.py:155-196` |
| Ed25519 CSR keys are accepted (and are the documented agent default) | `app/core/ca.py:_require_strong_key` |
| Gateway identity is `sha256(DER(client cert))` matched against `agents.cert_fingerprint`; unknown/revoked/expired closes with **4401**, revocation-while-connected closes with **4403** | `app/gateway/main.py:179-186,42`; `app/gateway/dispatch.py:120` |
| The gateway serves **any path**; the enrollment response's `gateway_url` is what to dial (`wss://host:port/agent` in practice) | `app/gateway/main.py:148-149` |
| Server → agent has exactly **one** message kind: `JobEnvelope`. There is no policy channel and no config channel | `app/gateway/protocol.py:150-153` |
| Agent → server kinds: `job_status` (`state` is `"running"` and nothing else), `job_log`, `job_result`, `heartbeat`, `security_events`, `health_report` | `app/gateway/protocol.py` |
| `sig` is **base64 (std, padded) Ed25519** over `canonical_bytes({id,type,device,params,issued_at,expires_at,nonce})` | `app/jobs/engine.py:_payload/_sign`, `app/core/signing.py` |
| `canonical_bytes` = `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False).encode("utf-8")` | `app/jobs/canonical.py` |
| `rfc3339` = `"%Y-%m-%dT%H:%M:%SZ"` — whole seconds, always `Z` | `app/jobs/canonical.py` |
| A `job_result` with `state:"refused"` is read as `result["reason"]` (default `"agent refused"`) | `app/gateway/dispatch.py:334-337` |
| `inventory.collect`'s result **is** the `InventoryDocument` at the top level, not nested | `app/modules/inventory/handler.py:27` |
| `net.sweep` jobs are signed with a **600 s** TTL | `docs/modules/ipam/spec.md` §5.2 |
| `scope_digest` = `sha256(json.dumps(targets, separators=(",",":")))` hex — the server hashes the list **as given**, without sorting | `app/modules/ipam/net.py:scope_digest` |
| A `health_report` whose `policy_version < acknowledged - 1` is discarded server-side | `app/modules/health/ingest.py:552` |
| `HealthReportMsg.reports` is bounded `1..64`; `value` is a **string**, `max_length=64` | `app/gateway/protocol.py:99-147` |
| `HeartbeatMsg` is **dropped on the floor** by the server today | `app/gateway/dispatch.py:290-291` |
| `scripts/demo_e2e_agent.py` already drives a real binary via `ITS_AGENT_BIN`, `ITS_AGENT_ENROLL_URL`, `ITS_AGENT_ENROLL_TOKEN`, `ITS_AGENT_DATA_DIR`, and expects `hello` + `inventory.collect` to succeed | `scripts/demo_e2e_agent.py:191-232` |
| CI has **three** jobs — `backend`, `deploy`, `frontend` — and no Go toolchain | `.github/workflows/ci.yml` |

### The invariants, restated as build rules

1. **No unsigned command path.** Not for tests, not for a local dev shortcut, not behind a build tag. The runner's output type is the *only* thing a capability accepts, and that type is unconstructable outside the `jobs` package. Every security test in this plan must state — and the builder must observe — **that the capability did not run**, not merely that an error was returned.
2. **Outbound only.** `net.Listen`, `net.ListenPacket`, `net.ListenUDP` and `http.Server` never appear in non-test agent code. Task 13's ICMP sockets are raw *sending* sockets opened inside a job and closed at its end; a raw socket that receives replies to probes this process sent is not a listening service, and Task 13 states the distinction and tests the absence of a bound TCP/UDP port.
3. **Credentials never at rest in the clear beyond what enrollment requires.** The device private key is written `0600` and never leaves the host, never appears in a log line, and never travels in a `job_log`.
4. **Audit is the server's.** The agent writes a local log; it does not attempt to produce audit records.
5/6. **Tenancy and RBAC are the server's.** The agent asserts no `org_id` and no permission. It reads its identity from its certificate and asserts nothing else.

### The idle budget is measured or it is not claimed

`CLAUDE.md` now requires a benchmark that runs in CI plus a stated falsifier before any footprint number is reported as verified. Task 9 builds the harness; Tasks 12 and 13 wire their capabilities into it. **A task that claims a footprint without a green assertion in `agent/internal/budget` is not done.**

A bare `go test -bench` does not fail a build — a benchmark that regresses still exits 0. Every budget claim in this plan is therefore asserted from an ordinary `Test...` function that calls `testing.Benchmark(...)` and asserts on the returned `AllocsPerOp`/`Bytes`. That is what "runs in CI" means here.

### Go house rules

- `agent/` is its own Go module (`module github.com/toeshee/it-suite/agent`). It does not import the backend and the backend does not import it.
- `gofmt -l` empty, `go vet ./...` clean, `staticcheck ./...` clean (`honnef.co/go/tools`, installed in CI only, not a module dependency).
- Table-driven tests. No `time.Sleep` in a test that can use an injected clock — the scheduler takes a `Clock` interface for exactly this reason.
- No global mutable state except the capability registry, which is written once at `init` and read-only thereafter.
- Every file that implements a wire contract carries a comment naming the schema file it implements and the spec section.

### Compatibility discipline (Rule 4, per task)

Every task that touches an envelope must state, in its commit message and in its report: **(a)** additive or versioned; **(b)** what an *old agent* does when it meets the *new server*; **(c)** what a *new agent* does when it meets an *old server*. Direction (c) is the one this repo has no precedent for and the one Task 11 nearly gets wrong — read that task before assuming (b) is the whole question.

### Control-plane changes (read this before sequencing)

Five tasks require a change to the Python control plane. In a single repo those land in the same commit as the agent change, which is precisely how a deployed agent breaks silently. Each is flagged in its task with the compatibility analysis and the order of deployment.

| Task | Control-plane change | Kind |
|---|---|---|
| 1 | `.github/workflows/ci.yml` gains an `agent` job | build only |
| 2 | `backend/tests/test_wire_schema_export.py` — assert the exported schemas are current | build only |
| 3 | `scripts/export_canonical_corpus.py` + a test that keeps the corpus current | build only |
| 4 | `app/gateway/dispatch.py` — `heartbeat` must refresh `agents.last_seen_at` | **behavioural, fixes a live defect** |
| 5 | `app/api/v1/enrollment.py` + `app/services/enrollment.py` — optional `capabilities` on `EnrollRequest` | **additive wire change** |
| 6 | **A certificate renewal path does not exist.** Not designed here; raised and blocked | **blocking gap** |
| 11 | `app/modules/ipam/nic.py` must read `attributes.addresses[]` | **behavioural, ordering-critical** |
| 13 | `scripts/export_scope_digest_corpus.py` | build only |
| 15 | `scripts/demo_e2e_agent.py` + doc references to `it-suite-agent` | docs/build |

---

## What the code contradicted

Six things found by reading the live code against the brief and the specs. Each is handled by a named task.

**C1 — `docs/05-agent-spec.md` says the agent lives in its own repository; `CLAUDE.md` (commit `a32cead`) says it lives under `agent/`.** A concurrent working-tree change to `05-agent-spec.md` and `docs/kernel/wire-protocol/README.md` is already reconciling this. Do not re-do that sweep. Task 15 covers only what those two files leave behind (`docs/modules/ipam/spec.md` §5.1/§5.5/§14.1, `app/modules/ipam/schema.py`'s docstring, `app/modules/health/jobtypes.py`'s docstring, `scripts/export_wire_schema.py`'s docstring, `scripts/demo_e2e_agent.py`).

**C2 — the heartbeat does nothing, and that opens a false `agent.offline` alert on every healthy device.** `handle_agent_message` returns immediately for `HeartbeatMsg` (`dispatch.py:290`). `agents.last_seen_at` is written only at connect and at disconnect (`gateway/main.py:260,281`). `health.sweep.run_offline_check` opens an `agent.offline` alert for any device whose `Agent.last_seen_at < now - offline_after_s`, and `offline_after_s` defaults to **900 seconds** (`health/models.py:556`). An agent that connects and stays connected for sixteen minutes is therefore reported offline while its socket is open. The brief said `health.check` was "fully built, tested and specced server-side"; this is the part of it that a real agent immediately falsifies. **Task 4 fixes it in the control plane and pins it with a backend test.**

**C3 — there is no certificate renewal path anywhere.** `grep -rn renew backend/app` returns only license renewals. Certificates are 30 days. `POST /enroll/v1` consumes a token, and a token is single-use or use-capped, so an agent cannot re-enroll itself. `docs/05-agent-spec.md` claims certificates are "short-lived and auto-rotated" — nothing implements that. **Task 6 raises this and does not work around it.** An agent fleet built by this plan stops connecting 30 days after enrollment unless a renewal path is designed first. This is the single highest-priority follow-on and it needs its own brainstorm/spec cycle, not an improvised endpoint in this plan.

**C4 — `agents.capabilities` is never populated by anything.** `docs/modules/ipam/spec.md` §5.3 says "it is written at enrollment by the agent". `EnrollRequest` has three fields and none of them is `capabilities`; `enrollment.enroll()` constructs `Agent(...)` without it. The column defaults to `{}`. IPAM's dispatcher already tolerates this (it dispatches anyway and treats a `refused` job as the answer), so the plan does **not** make capability advertisement a prerequisite. Task 5 adds it as a strictly additive, optional field, and Task 13 makes the refusal path correct regardless.

**C5 — Go's `encoding/json` cannot produce Python's canonical bytes.** Three concrete divergences: Go escapes `<`, `>`, `&` unless `SetEscapeHTML(false)`; Go emits ``/`` where Python emits `\b`/`\f`; Go escapes U+2028/U+2029, Python with `ensure_ascii=False` does not. Additionally, decoding into `any` turns every number into `float64`, which silently reformats integers beyond 2^53 and any float whose Go and Python reprs differ. **Task 3 writes a hand-rolled canonical encoder over a `json.Number`-preserving decode and pins it with a golden corpus generated from the Python function.** A near-miss here is not a bug that shows up as garbled output; it is every job in the fleet being refused, or worse, a canonicalisation that agrees on the corpus and diverges on a customer's params.

**C6 — "`scope_digest` has exactly one implementation" cannot be literally true across two languages, and the useful reading is narrower.** The server's rule (`app/modules/ipam/net.py`) is that the *dispatcher* and the *result handler* share one function so they cannot drift. The agent cannot call that function. What it must not do is **re-derive the input**: it hashes `params.targets` exactly as received — same order, same strings, no sorting, no collapsing, no normalising, no re-rendering of CIDRs — and echoes the result. Task 13 implements it that way, pins the hash function itself against a golden corpus generated from the Python one, and makes a disagreement between the agent's recomputation and `params.scope_digest` a **refusal before any packet is sent**, rather than a rejection after.

---

## File structure

**Created (agent, Go):**

| File | Responsibility | Task |
|---|---|---|
| `agent/go.mod`, `agent/go.sum` | module definition, two dependencies | 1 |
| `agent/cmd/agent/main.go` | process entry: config, enroll-if-needed, connect, serve, signal handling | 1, 4, 5 |
| `agent/internal/config/config.go` | env-driven config, data-dir layout | 1 |
| `agent/internal/wire/wire.go` | envelope structs for every kind in `docs/kernel/wire-protocol/` | 2 |
| `agent/internal/wire/schema_test.go` | every exported schema has a struct and round-trips | 2 |
| `agent/internal/canonical/canonical.go` | byte-exact port of `app/jobs/canonical.py` | 3 |
| `agent/internal/canonical/testdata/corpus.jsonl` | golden vectors generated from Python | 3 |
| `agent/internal/transport/transport.go` | mTLS WebSocket dial, read loop, write queue, backoff, heartbeat | 4 |
| `agent/internal/enroll/enroll.go` | keypair, CSR, `POST /enroll/v1`, identity persistence, pinning | 5 |
| `agent/internal/enroll/lifecycle.go` | certificate expiry awareness and the renewal gap | 6 |
| `agent/internal/jobs/verify.go` | the five checks; produces `VerifiedJob` | 7 |
| `agent/internal/jobs/runner.go` | dispatch to a capability, status/log/result reporting | 7, 8 |
| `agent/internal/jobs/nonce.go` | persisted bounded replay store | 8 |
| `agent/internal/jobs/buffer.go` | bounded offline outbound buffer, in-order flush | 8 |
| `agent/internal/scheduler/scheduler.go` | timer wheel, injected clock | 9 |
| `agent/internal/budget/budget.go`, `budget_test.go` | the falsifier harness and its assertions | 9 |
| `agent/internal/capabilities/registry.go` | job type → handler, dormant until invoked | 10 |
| `agent/internal/capabilities/inventory/*.go` | `inventory.collect`, per-OS collectors | 10, 11 |
| `agent/internal/capabilities/health/*.go` | `health.policy.apply`, `health.check.now`, tick engine | 12 |
| `agent/internal/capabilities/netsweep/*.go` | `net.sweep` | 13 |
| `agent/packaging/{windows,linux,darwin}/…` | service definitions and installers | 14 |

**Modified (control plane):** see the control-plane table above.

---

## Task 1: Go module, layout, and a CI job that can go red

**Files:**
- Create: `agent/go.mod`, `agent/cmd/agent/main.go`, `agent/internal/config/config.go`, `agent/internal/config/config_test.go`, `agent/.gitignore`, `agent/README.md`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: nothing.
- Produces: `config.Config{EnrollURL, EnrollToken, DataDir string}`; `config.Load() (Config, error)`; `config.Config.IdentityPath() string`, `.KeyPath() string`, `.PinsPath() string`, `.NoncePath() string`, `.BufferPath() string` — all rooted at `DataDir`.

- [ ] **Step 1: Create the module**

```bash
cd /home/user/Toeshee-RMS && mkdir -p agent/cmd/agent agent/internal/config
cd agent && go mod init github.com/toeshee/it-suite/agent && go mod edit -go=1.24
```

- [ ] **Step 2: Write the failing config test**

`agent/internal/config/config_test.go`. The env var names are **fixed by `scripts/demo_e2e_agent.py:194-198`** — do not rename them.

```go
func TestLoadRequiresEnrollURLOnFirstRun(t *testing.T) {
	t.Setenv("ITS_AGENT_DATA_DIR", t.TempDir())
	if _, err := config.Load(); err == nil {
		t.Fatal("want error when ITS_AGENT_ENROLL_URL is unset and no identity exists")
	}
}

func TestPathsAreRootedAtDataDir(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("ITS_AGENT_DATA_DIR", dir)
	t.Setenv("ITS_AGENT_ENROLL_URL", "http://127.0.0.1:8000")
	t.Setenv("ITS_AGENT_ENROLL_TOKEN", "tok")
	c, err := config.Load()
	if err != nil { t.Fatal(err) }
	for _, p := range []string{c.IdentityPath(), c.KeyPath(), c.PinsPath(), c.NoncePath(), c.BufferPath()} {
		if !strings.HasPrefix(p, dir) { t.Fatalf("%s escapes the data dir %s", p, dir) }
	}
}
```

- [ ] **Step 3: Run to verify failure** — `go test ./internal/config/` fails to build: `undefined: config.Load`.

- [ ] **Step 4: Implement `config.Load`**

`ITS_AGENT_DATA_DIR` defaults to `/var/lib/its-agent` on Linux/macOS and `%ProgramData%\ITSuite\agent` on Windows. `Load` returns an error only if there is no persisted identity *and* no enrollment URL+token — an already-enrolled agent needs neither.

- [ ] **Step 5: Minimal `main.go`**

Parses config, prints its version, exits 0. It grows in Tasks 4-9. Add a `//go:build` free `version` var set by `-ldflags`.

- [ ] **Step 6: Add the CI job**

Append to `.github/workflows/ci.yml`, as a fourth top-level job:

```yaml
  agent:
    name: agent (go test · vet · staticcheck · idle budget)
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: agent
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: "1.24"
          cache-dependency-path: agent/go.sum
      - name: gofmt
        run: test -z "$(gofmt -l .)" || { gofmt -l .; exit 1; }
      - name: vet
        run: go vet ./...
      - name: staticcheck
        run: |
          go install honnef.co/go/tools/cmd/staticcheck@2025.1.1
          "$(go env GOPATH)/bin/staticcheck" ./...
      - name: test
        run: go test ./... -count=1
      - name: cross-compile
        run: |
          for t in windows/amd64 windows/arm64 linux/amd64 linux/arm64 darwin/amd64 darwin/arm64; do
            GOOS=${t%/*} GOARCH=${t#*/} CGO_ENABLED=0 go build -o /dev/null ./cmd/agent || exit 1
          done
```

**Control-plane change (build only).** Compatibility: none — the workflow is not a wire contract. But note what this job's *existence* means: from now on a control-plane change that breaks the agent goes red here. That is the whole point of `CLAUDE.md`'s "a green backend suite says nothing about the agent".

- [ ] **Step 7: Prove the CI job can fail**

Introduce a deliberate `go vet` error (`fmt.Printf("%d", "x")`), push nothing but run the job's commands locally, watch `go vet` exit non-zero, then revert. Paste the output. A CI job nobody has ever seen fail is a decoration.

**Definition of done:** `cd agent && go build ./... && go vet ./... && go test ./...` all clean; the six cross-compile targets build; the `agent` job is in `ci.yml`; the vet-failure proof is pasted.

Commit: `feat(agent): go module, config, and a CI job that runs the agent's tests`

---

## Task 2: Wire types, generated against the exported schemas — plus a guard that the export is current

**Files:**
- Create: `agent/internal/wire/wire.go`, `agent/internal/wire/wire_test.go`, `agent/internal/wire/schema_test.go`
- Create (control plane): `backend/tests/test_wire_schema_export.py`

**Interfaces:**
- Consumes: `agent/internal/config`.
- Produces: `wire.Envelope` (the tagged server→agent frame), `wire.JobPayload{ID, Type, Device, Params json.RawMessage, IssuedAt, ExpiresAt, Nonce string}`, `wire.JobStatus`, `wire.JobLog`, `wire.JobResult`, `wire.Heartbeat`, `wire.HealthReport`, `wire.HealthReportLine`, `wire.SecurityEvents`; `wire.Decode([]byte) (any, error)`; `wire.Encode(msg any) ([]byte, error)`.

- [ ] **Step 1: Read the schemas, not the Python**

`ls docs/kernel/wire-protocol/*.schema.json`. These are the compatibility boundary. Build the structs from them.

- [ ] **Step 2: Write the failing schema-coverage test**

`agent/internal/wire/schema_test.go` walks `../../../docs/kernel/wire-protocol/`, and for every `*.schema.json` whose `title` names a message kind this agent handles, asserts a Go type is registered for it and that every `required` property maps to a Go field with the right JSON tag. Types the agent does not implement yet (`BackupRunParams`, `PatchApplyParams`, …) go in an explicit `notImplemented` set with a one-line reason each — an explicit list, so adding a schema file forces a decision rather than being silently ignored.

```go
func TestEveryExportedSchemaIsAccountedFor(t *testing.T) {
	files, _ := filepath.Glob("../../../docs/kernel/wire-protocol/*.schema.json")
	if len(files) < 20 { t.Fatalf("only %d schemas found; wrong path?", len(files)) }
	for _, f := range files {
		title := readTitle(t, f)
		if _, ok := implemented[title]; ok { continue }
		if reason, ok := notImplemented[title]; ok && reason != "" { continue }
		t.Errorf("schema %s is neither implemented nor listed as deliberately unimplemented", title)
	}
}
```

- [ ] **Step 3: Write the failing round-trip test**

For each implemented type, decode the example from its schema's `required` fields, re-encode, and assert the JSON key set is exactly the schema's `properties` minus omitted optionals. Assert `Decode` rejects `{"v":2,...}` and `{"kind":"nope"}` — the server rejects both (`protocol.py`'s `Literal[1]` and the discriminated union), and an agent that accepts what the server rejects will one day act on something the server would not have sent.

- [ ] **Step 4: Run to verify failure.**

- [ ] **Step 5: Implement `wire.go`**

Two decisions that matter:

- **`JobPayload.Params` is `json.RawMessage`.** The agent must not round-trip params through `map[string]any` before verification — Task 3 canonicalises the *decoded* value, and every intermediate representation is a chance to change bytes. Each capability unmarshals `Params` into its own typed struct **after** verification.
- **Unknown fields are ignored on inbound, never emitted on outbound.** `_Msg` is `extra="forbid"` server-side, so an agent that emits an extra key gets its whole message dropped at `parse_agent_message` and the failure is a silent `logger.warning` on the server (`gateway/main.py:200-202`). Write a test that encodes each outbound type and asserts the key set is exactly what the schema permits.

- [ ] **Step 6: Control-plane guard — the export must be current**

`backend/tests/test_wire_schema_export.py`:

```python
def test_exported_wire_schemas_match_the_models(tmp_path):
    """Regenerating the export must produce no diff.

    If this guard were deleted, the observation is a `docs/kernel/wire-protocol/`
    file whose JSON differs from the model it claims to export -- and since the
    agent builds its Go structs from those files, the agent would be built
    against a contract the server no longer speaks. Assert on file CONTENT, not
    on the script exiting zero.
    """
```

Run `scripts/export_wire_schema.py` into a temp dir (parameterise `OUT_DIR` via an env var or an argument — a small, additive change to that script) and compare bytes with the committed files. At plan time the committed files **are** current (verified by regenerating: no diff), so this test passes on arrival and only ever fires on a real drift.

- [ ] **Step 7: Prove the guard can fail** — edit one committed `.schema.json` by a byte, watch the test go red naming the file, revert. Paste the output.

**Compatibility:** additive/build-only. An old agent is unaffected (nothing on the wire changed). A new agent gains a structural reason to trust the schema files.

**Definition of done:** every exported schema is implemented or explicitly excused; round-trip and rejection tests green; the backend export guard is green and has been observed red.

Commit: `feat(agent): wire types built from the exported schemas, plus an export-drift guard`

---

## Task 3: Canonical JSON — byte-exact, or every job is refused

**Files:**
- Create: `agent/internal/canonical/canonical.go`, `agent/internal/canonical/canonical_test.go`, `agent/internal/canonical/testdata/corpus.jsonl`
- Create (control plane): `scripts/export_canonical_corpus.py`, `backend/tests/test_canonical_corpus.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `canonical.Bytes(v any) ([]byte, error)`; `canonical.BytesFromRaw(raw json.RawMessage) ([]byte, error)`.

**Read C5 before writing a line of this.** Go's `encoding/json` is not a substitute here, and the failure mode of "close enough" is a fleet that refuses every job.

- [ ] **Step 1: Generate the golden corpus (control plane)**

`scripts/export_canonical_corpus.py` writes `agent/internal/canonical/testdata/corpus.jsonl`, one JSON object per line:

```python
{"input": <the value, as JSON>, "canonical": "<base64 of canonical_bytes(value)>"}
```

The corpus must contain, at minimum, and each for a stated reason:

| Case | Why |
|---|---|
| `{}`, `{"a":1}` | the floor |
| `{"b":1,"a":2,"A":3,"á":4,"à":5}` | key sort order. Python sorts by code point; Go sorts UTF-8 bytes; those agree, and the corpus is what proves it rather than the argument |
| `{"s":"<script>&amp;</script>"}` | Go escapes `<>&` by default; Python does not |
| `{"s":"a\bb\fc"}` | Python emits `\b`/`\f`; Go emits ``/`` |
| `{"s":"line sep "}` | Go escapes these; Python with `ensure_ascii=False` does not |
| `{"s":"日本語 — ✓"}` | non-ASCII passes through unescaped |
| `{"s":"quote\" back\\ tab\t nl\n"}` | the short forms both agree on |
| `{"s":" "}` | control chars below the short-form set |
| `{"n":0,"m":-0.0,"big":9007199254740993,"f":1.5,"e":1e21,"tiny":1e-7}` | number rendering. `9007199254740993` is the one `float64` cannot hold |
| `{"t":true,"f":false,"z":null}` | literals |
| `{"a":[1,{"b":[]},[]],"deep":{"x":{"y":{"z":1}}}}` | nesting and empty containers |
| a realistic `NetSweepParams` and a realistic `HealthPolicyApplyParams` payload | the two params shapes this plan actually ships |

`backend/tests/test_canonical_corpus.py` asserts the committed corpus is exactly what the current `canonical_bytes` produces.

```python
def test_canonical_corpus_is_current():
    """The Go agent's canonical encoder is pinned to this file.

    If this guard were deleted, the observation is a corpus whose `canonical`
    values disagree with `app/jobs/canonical.py` -- and the Go side would keep
    passing its own tests while producing bytes the server never signed. Every
    job in the fleet would then be REFUSED, with no test in this repository red.
    """
```

**This is a control-plane change of the build-only kind, but it is the most important one in the plan.** `canonical_bytes` is now a cross-language contract: changing it is a fleet-wide break with no versioning story. Add a comment saying so at the top of `app/jobs/canonical.py`.

- [ ] **Step 2: Write the failing Go test**

`canonical_test.go` reads `testdata/corpus.jsonl`, decodes `input` with `json.Decoder` + `UseNumber()`, calls `canonical.Bytes`, and asserts byte equality against the decoded base64. One subtest per line, named by index, so a failure names the case.

- [ ] **Step 3: Run to verify failure** — `undefined: canonical.Bytes`.

- [ ] **Step 4: Implement the encoder**

Hand-rolled. Do not delegate to `encoding/json` for strings or numbers.

```go
// Bytes reproduces app/jobs/canonical.py::canonical_bytes exactly:
//   json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
// encoded UTF-8. Pinned by testdata/corpus.jsonl, which is generated from the
// Python function. See docs/modules/agent/plan.md C5 for the three ways
// encoding/json diverges.
func Bytes(v any) ([]byte, error)
```

Rules, each traceable to CPython's `json.encoder`:
- objects: keys sorted by `<` on the Go `string` (byte order == code point order for valid UTF-8); `{"k":v,"k2":v2}`, no spaces.
- strings: emit raw UTF-8 except `"` → `\"`, `\` → `\\`, `\n` `\r` `\t` `\b` `\f` as their short forms, any other codepoint `< 0x20` as `\u00xx` lowercase hex. **Nothing else is escaped** — not `<`, `>`, `&`, `/`, U+2028, U+2029, or any non-ASCII.
- numbers: the input arrives as `json.Number`; emit its `.String()` **verbatim**. The server produced the wire text and the canonical text from the same Python object, so the received token is already the right bytes. `Bytes` must reject a Go `float64`/`int` input that did not come from a `json.Number` — take that path away rather than guess at Python's `repr`.
- `true`, `false`, `null` literally.
- `BytesFromRaw` is the entry point Task 7 uses: decode with `UseNumber()` into `any`, then `Bytes`.

- [ ] **Step 5: Run tests to verify they pass** — every corpus line green.

- [ ] **Step 6: Fuzz it**

`FuzzCanonicalMatchesCorpusInvariants`: for arbitrary valid JSON input, assert `Bytes` output re-parses to a value deeply equal to the input, and that `Bytes(Bytes-parsed)` is idempotent. Seed the corpus. Run `go test -fuzz=Fuzz -fuzztime=60s` locally once, commit any crashers found to `testdata/fuzz/`, and leave the fuzz target in the suite (it runs its seeds under plain `go test`).

- [ ] **Step 7: Prove the corpus test can fail**

Change one byte of one `canonical` value in the committed corpus, run the Go test — it must go red naming that index — and run the Python guard — it must go red naming the line. Restore. Paste both.

**Definition of done:** every corpus case byte-equal; fuzz seeds green; both falsification proofs pasted; the note added to `app/jobs/canonical.py`.

Commit: `feat(agent): byte-exact canonical JSON, pinned to a corpus generated from the signer`

---

## Task 4: mTLS WebSocket transport — and the heartbeat defect it exposes

**Files:**
- Create: `agent/internal/transport/transport.go`, `agent/internal/transport/transport_test.go`, `agent/internal/transport/listen_test.go`
- Modify (control plane): `backend/app/gateway/dispatch.py`; Test: `backend/tests/test_gateway_heartbeat.py`

**Interfaces:**
- Consumes: `wire`, `config`.
- Produces: `transport.Conn` with `Recv(ctx) ([]byte, error)`, `Send(ctx, msg any) error`, `Close() error`; `transport.Dial(ctx, transport.Options{GatewayURL string, ClientCert tls.Certificate, RootCAs *x509.CertPool, HeartbeatEvery time.Duration}) (*Conn, error)`; `transport.Run(ctx, opts, handler func(context.Context, []byte)) error` — the reconnect loop.

- [ ] **Step 1: Write the failing transport tests**

Stand up a `httptest.NewUnstartedServer` with `TLSConfig{ClientAuth: tls.RequireAndVerifyClientCert, MinVersion: tls.VersionTLS13}` and a `coder/websocket` accept handler. Assert:
- a dial with the right client cert succeeds and receives a pushed frame;
- a dial without a client cert fails;
- a dial to a server whose cert is not in `RootCAs` fails **before** any frame is sent (pinning is not advisory);
- `Run` reconnects after the server closes, with backoff: record the connect timestamps against an injected clock and assert the delays are `min(cap, base*2^n)` with jitter inside `[0.5x, 1.5x]`, and that close code **4401** (`CLOSE_UNAUTHORIZED`) and **4403** (`CLOSE_REVOKED`) are **terminal** — an agent that has been revoked must stop reconnecting, not hammer the gateway forever;
- exactly one goroutine is blocked on the read, and `Recv` does not poll: assert `runtime.NumGoroutine()` is constant across a 200 ms idle window with the injected clock advanced by an hour.

- [ ] **Step 2: The no-listener test (invariant #2)**

`listen_test.go`:

```go
func TestAgentOpensNoListeningSocket(t *testing.T) {
	// Falsifying observation: if the agent ever bound a port, this test
	// reports the extra LISTEN entry with its port number. "No error was
	// returned" is not an observation; a bound port is.
	before := listeningPorts(t)
	conn := startAgentTransportAgainstFakeGateway(t)
	defer conn.Close()
	after := listeningPorts(t)
	if diff := added(before, after); len(diff) != 0 {
		t.Fatalf("agent bound %v; invariant #2 forbids any listening port", diff)
	}
}
```

`listeningPorts` reads `/proc/net/tcp` + `/proc/net/tcp6` on Linux and skips elsewhere with `t.Skip` (CI is Linux). Add a companion source-level test that walks the module's non-`_test.go` files with `go/ast` and fails on any call to `net.Listen`, `net.ListenTCP`, `net.ListenUDP`, `net.ListenPacket`, or a composite literal of `http.Server`. Task 13 will add exactly one allowance to that walker, in the open, with a comment.

- [ ] **Step 3: Run to verify failure.**

- [ ] **Step 4: Implement the transport**

`tls.Config{MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{clientCert}, RootCAs: pinnedPool}`. Dial the `gateway_url` from enrollment. One reader goroutine on `conn.Read`; one writer goroutine draining a buffered channel — never write from two goroutines, `coder/websocket` forbids it. Heartbeat on a `time.Ticker` at 60 s (see the control-plane change below for why the value matters).

- [ ] **Step 5: Control-plane change — make the heartbeat mean something (C2)**

`app/gateway/dispatch.py::handle_agent_message` currently is:

```python
if isinstance(msg, HeartbeatMsg):
    return
```

It must instead refresh the agent's liveness inside an `org_session`:

```python
if isinstance(msg, HeartbeatMsg):
    await _touch_agent(app_engine, device_id=device_id, org_id=org_id)
    return
```

`_touch_agent` issues a single `UPDATE agents SET last_seen_at = now() WHERE device_id = :device_id` under the tenant session. **No audit record** — this is per-minute per-device volume and `_handle_health_report`'s docstring already states the rule ("no per-report audit record; volume would swamp a hash-chained log whose value is that a human can read it"). Rate-limit it in the gateway to at most once per 60 s per device with an in-process `dict[UUID, datetime]` so a chatty or hostile agent cannot turn the heartbeat into a write amplifier; the agent's own 60 s ticker means the common case is one write per minute either way.

`backend/tests/test_gateway_heartbeat.py`:

```python
async def test_heartbeat_advances_last_seen_at(api_env):
    """C2: agents.last_seen_at is written only at connect and disconnect, so a
    healthy long-lived connection ages past health_org_settings.offline_after_s
    (default 900s) and run_offline_check opens a false agent.offline alert.

    If this guard were deleted, the observation is a device whose socket is
    open, whose Agent row still carries the connect-time timestamp 20 minutes
    later, and for which run_offline_check has opened an alert. Assert on the
    stored timestamp AND on the alert count -- not on 'no exception'.
    """
```

The second half of that test is the one that matters: seed an agent connected 20 minutes ago, deliver a heartbeat through `handle_agent_message`, run `run_offline_check`, and assert **zero** `health_alert` rows opened. Without the fix it opens one.

**Compatibility:** additive and safe in both directions. *Old agent → new server*: `HeartbeatMsg` already exists in the v1 protocol and the shipped Python demo agent sends it; nothing new is required of the agent, so an old agent that heartbeats gets the fix for free, and one that does not is exactly as broken as it is today. *New agent → old server*: the old server ignores heartbeats — the agent stays connected and works; only `agent.offline` is wrong, which is today's behaviour. Nothing is required in a particular order.

- [ ] **Step 6: Run both suites** — `cd agent && go test ./internal/transport/` and `cd backend && .venv/bin/pytest tests/test_gateway_heartbeat.py -v`.

- [ ] **Step 7: Prove the offline-alert assertion can fail** — revert `_touch_agent`, watch the test report one opened alert, restore. Paste it.

**Definition of done:** all transport tests green; the no-listener test green and the AST walker in place; the control-plane heartbeat fix shipped with its test and the falsification pasted; report the exact behavioural change and its compatibility analysis in the commit body.

Commit: `feat(agent): mTLS websocket transport; fix(gateway): heartbeats must refresh agents.last_seen_at`

---

## Task 5: Enrollment, identity, and pinning

**Files:**
- Create: `agent/internal/enroll/enroll.go`, `agent/internal/enroll/enroll_test.go`
- Modify: `agent/cmd/agent/main.go`
- Modify (control plane, additive): `backend/app/api/v1/enrollment.py`, `backend/app/services/enrollment.py`; Test: append to `backend/tests/test_enrollment.py`

**Interfaces:**
- Consumes: `config`, `transport`.
- Produces: `enroll.Identity{DeviceID string, Cert tls.Certificate, CACerts *x509.CertPool, GatewayURL string, JobSigningKey ed25519.PublicKey}`; `enroll.LoadOrEnroll(ctx, config.Config) (*Identity, error)`.

- [ ] **Step 1: Write the failing enrollment tests**

Against an `httptest` server that mimics `POST /enroll/v1`:
- a fresh data dir produces a CSR, POSTs it, and persists cert + key + pins;
- **the private key file is mode `0600` and its bytes never appear in any log line** — capture the agent's logger into a buffer and assert `!bytes.Contains(buf.Bytes(), keyPEM)` and that the PEM header string `PRIVATE KEY` is absent. Falsifying observation: the key material appears in the captured log buffer;
- a second `LoadOrEnroll` on a populated data dir performs **zero** HTTP requests (assert the fake server's request counter is 0) — an agent that re-enrolls on restart burns a token per reboot;
- a **403** response returns a typed `enroll.Rejected` error and writes nothing to the data dir (assert the dir is still empty; a partial identity is worse than none);
- the pinned `job_signing_public_key_pem` parses as Ed25519 and a non-Ed25519 PEM is rejected at enrollment time, not at first job.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

- Generate **Ed25519** (accepted by `_require_strong_key`, and the documented agent default).
- CSR subject can be empty — the CA overwrites CN/O from `device_id`/`org_id` (`ca.py:157-168`) and does not read the CSR subject. Do not put a hostname in it; it would be ignored and would read as meaningful.
- Device facts: `hostname`, `os_family` ∈ `{windows,linux,macos}` (the server's pattern), `os_version`, `architecture`, `serial`. `os_family` must be derived from `runtime.GOOS` with an explicit map — `darwin` → `macos` is the one that bites.
- POST to `EnrollURL + "/enroll/v1"`. **Not** `/api/v1/enroll/v1`. Write a test asserting the exact path, because this is the single most guessable-wrong fact in the plan.
- Persist: `identity.json` (`device_id`, `gateway_url`, cert PEM, chain PEM), `agent.key` (0600), `pins.json` (`ca_bundle_pem`, `job_signing_public_key_pem`). Create the data dir `0700`.
- Build the `tls.Certificate` from cert+key and the `x509.CertPool` from `ca_bundle_pem`.

- [ ] **Step 4: Wire `main.go`** — `LoadOrEnroll`, then `transport.Run` against `Identity.GatewayURL`. At this point `scripts/demo_e2e_agent.py` should get as far as "device enrolled and online" (the `hello` job still fails; that is Task 7).

- [ ] **Step 5: Run the demo as an integration proof**

```bash
cd agent && CGO_ENABLED=0 go build -o /tmp/its-agent ./cmd/agent
cd /home/user/Toeshee-RMS && ITS_AGENT_BIN=/tmp/its-agent backend/.venv/bin/python scripts/demo_e2e_agent.py
```

Expect it to reach `device … enrolled and online` and then fail waiting on `hello`. Paste that output — it is the first end-to-end proof that the mTLS identity the CA issued is the one the gateway resolves.

- [ ] **Step 6: Control-plane change — optional capability advertisement (C4)**

Add to `EnrollRequest`:

```python
capabilities: dict[str, Any] = Field(default_factory=dict)
```

and pass it through `svc.enroll(..., capabilities=...)` into `Agent(capabilities=capabilities or {})`. The agent sends `{"net.sweep": true, "inventory.collect": true, "health.check": true}` — the job types it actually handles, from the registry, so the advertisement cannot drift from the code.

**Compatibility:** strictly additive, and the direction that matters is the unusual one. *Old agent → new server*: the field defaults to `{}`, exactly today's stored value, and IPAM's dispatcher already tolerates an empty map by dispatching and treating `refused` as the answer (`spec.md` §5.3). *New agent → old server*: an old `EnrollRequest` is a Pydantic model with default `extra` behaviour (`ignore`), so an unknown `capabilities` key is dropped and enrollment still succeeds — **verify this before shipping**; if that model is ever tightened to `extra="forbid"`, a new agent would fail to enroll against an old control plane, which is an outage in the direction nobody checks. Add a test asserting `EnrollRequest` tolerates an unknown key, so a future tightening goes red here.

- [ ] **Step 7: Run the backend suite** — `cd backend && .venv/bin/pytest tests/test_enrollment.py -v`, then the full suite once (you touched a shipped request model).

**Definition of done:** all enrollment tests green including the key-material-never-logged assertion; the demo reaches "enrolled and online"; the additive `capabilities` field shipped with both compatibility tests.

Commit: `feat(agent): enrollment, identity persistence, and pinning; feat(enrollment): optional capability advertisement`

---

## Task 6: Certificate lifecycle — and the renewal gap, raised not worked around

**Files:**
- Create: `agent/internal/enroll/lifecycle.go`, `agent/internal/enroll/lifecycle_test.go`
- Create: `docs/modules/agent/open-questions.md`

**Interfaces:**
- Consumes: `enroll.Identity`.
- Produces: `enroll.ExpiryState(now time.Time, id *Identity) (remaining time.Duration, state string)` where `state` ∈ `{ok, expiring, expired}`.

**Read C3 first. This task does not build a renewal path.**

`app/core/ca.py` issues agent certificates with `ttl_days=30`. There is no renewal endpoint, no rotation job type, and no re-enrollment path an already-enrolled agent can use (`POST /enroll/v1` consumes an enrollment token, and tokens are use-capped and expire). `docs/05-agent-spec.md` claims certificates are "short-lived and auto-rotated". **Nothing implements the second half.** Every agent this plan deploys stops connecting 30 days after enrollment, and the gateway will close it 4401.

`CLAUDE.md`'s "when in doubt" says to stop and raise rather than work around. Improvising a renewal endpoint inside an implementation plan would be designing a new authenticated path into the fleet's identity system in the worst possible place. So:

- [ ] **Step 1: Write the failing expiry test**

```go
func TestExpiryStateCrossesThresholdsFromTheCertificate(t *testing.T) {
	// Reads NotAfter from the parsed leaf. A 30-day cert is `expiring` from
	// day 20 (one third of life remaining) and `expired` after NotAfter.
}
```

- [ ] **Step 2: Implement `ExpiryState`** — parse the leaf, compare against an injected `now`.

- [ ] **Step 3: Surface it, loudly, on the paths that exist**

- Log at WARN once per connect while `expiring`, at ERROR while `expired`, naming the exact `NotAfter`.
- Send a `job_log`-shaped notice? **No** — `JobLogMsg` requires a `job_id` bound to a real job for this device (`_load_job_for_device`), so there is no unsolicited log channel. Do not invent one; that is a second server→agent-adjacent path with no verification story. The local log and the operator's device list (`agents.cert_not_after` is already stored) are the surfaces that exist.
- Add `agent/internal/budget`-free, zero-cost checking: the expiry check runs at connect and on the daily scheduler tick, not on a timer of its own.

- [ ] **Step 4: Write the gap up**

`docs/modules/agent/open-questions.md`, one entry:

> **OQ-1 — certificate renewal does not exist (blocking, 30-day fuse).** State the facts (30-day TTL, no endpoint, tokens are single-use), the three candidate designs and what each costs — (a) an mTLS-authenticated `POST /enroll/v1/renew` on the API listener, which needs the API to terminate client certs it does not terminate today; (b) a `agent.cert.renew` signed job carrying nothing, whose result is a CSR, plus a follow-up signed job carrying the issued cert — two round trips, but it reuses the signed-job path verbatim and adds no new authenticated surface; (c) renewal over the gateway connection as a new message kind, which is a new server→agent instruction path and would need its own verification story before it could be considered. Recommend (b), state that it needs a brainstorming/spec cycle, and note that it changes `docs/kernel/wire-protocol/` and is therefore a compatibility decision.

- [ ] **Step 5: Report it as a blocker** — this goes in the task report and in the commit body, not only in a doc.

**Definition of done:** expiry state tested and surfaced; OQ-1 written with the three options and a recommendation; the blocker reported. **No renewal endpoint is built in this task.**

Commit: `feat(agent): certificate expiry awareness; docs(agent): raise the renewal gap as blocking`

---

## Task 7: The signed-job runner — the one thing this plan cannot get wrong

**Files:**
- Create: `agent/internal/jobs/verify.go`, `agent/internal/jobs/verify_test.go`, `agent/internal/jobs/runner.go`, `agent/internal/jobs/verified.go`
- Create: `agent/internal/jobs/construct_test.go`

**Interfaces:**
- Consumes: `wire`, `canonical`, `enroll.Identity`, `capabilities.Registry` (Task 10 fills it; here it is an interface with a test double).
- Produces:
  ```go
  type VerifiedJob struct {  // in verified.go
      ID, Type, Device string
      Params  json.RawMessage
      IssuedAt, ExpiresAt time.Time
      verified struct{}     // unexported, zero-size: only this package can set it
  }
  type Verifier struct{ ... }
  func (v *Verifier) Verify(env *wire.Envelope, now time.Time) (*VerifiedJob, RefusalReason, error)
  func (r *Runner) Handle(ctx context.Context, frame []byte)
  ```
  `RefusalReason` is a string enum: `bad_signature`, `wrong_device`, `expired`, `not_yet_valid`, `replayed_nonce`, `unknown_type`, `malformed`.

**The structural guarantee.** `VerifiedJob` carries an unexported zero-size field. Go permits a composite literal with unexported fields **only inside the declaring package**, so no capability package can manufacture one. Capability handlers take `*jobs.VerifiedJob`. That is the type system enforcing invariant #1, and it is stronger than a code review.

- [ ] **Step 1: Write the failing verification tests**

Every case runs the **whole** `Runner.Handle` path against a registry whose sole capability is a `spyCapability` that increments a counter and writes a marker file. The assertion in every negative case is:

```go
if spy.calls != 0 {
    t.Fatalf("capability RAN for a %s job: invariant #1 breached", tc.name)
}
if _, err := os.Stat(spy.markerPath); err == nil {
    t.Fatal("capability produced its side effect for a rejected job")
}
```

**That counter and that marker file are the falsifying observation.** "An error was returned" is not one: a runner that returns an error *after* invoking the capability passes an error-shaped assertion and fails the invariant.

The cases, each also asserting a `job_result` with `state:"refused"` and the right `result["reason"]` reached the fake transport:

| Case | Construction |
|---|---|
| valid | signed by the test key over `canonical.Bytes(job)`; spy **must** run exactly once |
| tampered params | take a valid envelope, flip one byte inside `params` (e.g. `"rate_pps":50` → `51`), leave `sig` untouched |
| tampered type | change `"type":"hello"` → `"script.run"`, leave `sig` untouched — the escalation shape |
| tampered device | change `job.device` to another UUID, re-sign with the **real** key. Signature is valid; identity is not. This is the case a signature-only check passes |
| absent signature | `"sig": ""` |
| signature by a different key | sign with a second Ed25519 key not pinned |
| signature over non-canonical bytes | sign `json.dumps(job)` with default separators — the exact bug a naive Go port produces (see C5) |
| expired | `expires_at` one second in the past |
| not yet valid | `issued_at` ten minutes in the future (beyond the allowed skew) |
| replayed nonce | deliver a valid envelope twice; the second must refuse `replayed_nonce` and the spy count must stay at 1 |
| replayed nonce across restart | deliver, construct a fresh `Verifier` over the same data dir, deliver again |
| unknown job type | `"type":"patch.apply"` with no registered handler |
| wrong version | `"v":2` |
| wrong kind | `"kind":"policy"` — proving there is no second instruction path |
| malformed JSON | truncated frame |

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement `Verify`, in this order**

1. Envelope parses, `v == 1`, `kind == "job"`.
2. **Signature.** `canonical.BytesFromRaw` over the `job` object as a whole (decode the raw `job` sub-object with `UseNumber()`, canonicalise, `ed25519.Verify(pinnedKey, msg, sigBytes)`), `sig` decoded with `base64.StdEncoding`.
3. **Device.** `job.device == identity.DeviceID`, compared as an exact string. Do not normalise UUID case — the server sends `str(job.target_id)`, lowercase canonical; an agent that case-folds is inventing an equivalence the server never promised.
4. **Freshness.** Parse `issued_at`/`expires_at` with `time.Parse(time.RFC3339, …)`. Allow **±120 s** clock skew on `issued_at`, none on `expires_at`. State the skew constant with a comment: it exists because endpoints drift, and it is deliberately smaller than the shortest job TTL in the platform (`net.sweep`'s 600 s).
5. **Nonce.** `nonce.SeenOrRecord(job.nonce, job.expires_at)` (Task 8). Record **after** every other check passes, so a forged envelope cannot poison a legitimate nonce.
6. **Type.** `registry.Lookup(job.type)`; unknown → refuse.

Signature before device on purpose: an unsigned envelope's `device` field is attacker-controlled and must never be used to make a decision, not even to decide which error to report.

- [ ] **Step 4: Implement `Runner.Handle`**

On success: send `job_status{state:"running"}`, invoke the capability with the `*VerifiedJob` and a context bounded by `expires_at`, then send `job_result{state:"succeeded"|"failed", result:…}`. A panicking capability is recovered and reported `failed` — one bad capability must not kill the connection, mirroring the gateway's own `except Exception` around `handle_agent_message`.

On refusal: send `job_result{state:"refused", result:{"reason":"<RefusalReason>: <detail>"}}`. `dispatch.py:334-337` reads `result["reason"]` and `JobEngine.refuse` audits it, so the reason string is what an operator will see in `jobs.refused`. Keep it short, specific, and free of any attacker-controlled text beyond the job type — do not echo `params` into an audit record.

- [ ] **Step 5: The unconstructable-type test**

`agent/internal/jobs/construct_test.go` is in package `jobs_test` (external) and uses `go/types` via `golang.org/x/tools`… **no** — that adds a dependency. Instead: a `testdata/badclient/main.go` that attempts `jobs.VerifiedJob{ID: "x"}` from another package, plus a test that runs `go build ./testdata/badclient` with `exec.Command` and asserts it **fails** with a message mentioning `verified`. Falsifying observation: the build succeeds, meaning a capability package could forge a verified job.

- [ ] **Step 6: Run everything, then run the demo**

`go test ./internal/jobs/ -v -count=1`, then rebuild `/tmp/its-agent` and run `scripts/demo_e2e_agent.py` — the `hello` job must now succeed with `{"greeting": "hello from the control plane"}`. Paste that output.

- [ ] **Step 7: Falsify the invariant test itself**

Delete the signature check (`return &VerifiedJob{...}, "", nil` before the `ed25519.Verify`), run the suite, and confirm the **tampered params**, **tampered type**, **absent signature**, **different key** and **non-canonical bytes** cases all go red *reporting `spy.calls == 1`*. Restore. Paste the failure output. If any of those five still passes with the check deleted, that case is decoration — fix it before continuing.

**Definition of done:** all fifteen verification cases green; the spy counter is the assertion in every negative case; the unconstructable-type build test green; the `hello` job succeeds end-to-end; the five-case falsification pasted.

Commit: `feat(agent): signed-job runner -- verify signature, device, freshness, nonce, type before anything runs`

---

## Task 8: Replay store, outbound buffer, and reporting

**Files:**
- Create: `agent/internal/jobs/nonce.go`, `agent/internal/jobs/nonce_test.go`, `agent/internal/jobs/buffer.go`, `agent/internal/jobs/buffer_test.go`

**Interfaces:**
- Consumes: `config`, `transport`, `wire`.
- Produces: `jobs.NonceStore` with `SeenOrRecord(nonce string, expiresAt time.Time) bool`, `Prune(now time.Time) int`; `jobs.Buffer` with `Enqueue(msg any) error`, `Flush(ctx, *transport.Conn) error`, `Len() int`.

- [ ] **Step 1: Write the failing nonce tests**

- record-then-check is `true` on the second call;
- survives a process restart: write, construct a new store over the same path, check;
- `Prune` removes entries whose `expiresAt` is in the past **and only those** — the store is bounded by the fact that freshness already rejects anything older, so an expired nonce can never be replayed usefully;
- the store is bounded at 10,000 entries; at the cap it evicts the **soonest-expiring** first and `SeenOrRecord` returns `true` (treat as seen → refuse) if it cannot record. **Falsifying observation for that last one:** with the fail-closed branch removed, a flood of 10,001 jobs lets job #10,001's nonce be replayed and executed — assert the spy counter, not the return value;
- the file is written atomically (temp + rename) and a corrupt file is discarded with a WARN rather than crashing the agent into a boot loop. A corrupt nonce store that fails open would silently disable replay protection: assert that a corrupt file leads to an **empty** store, and pair it with a log assertion, so "we lost the history" is visible.

- [ ] **Step 2: Write the failing buffer tests**

- FIFO order preserved across a disconnect and a restart;
- bounded by **total bytes** (8 MiB default), dropping **oldest** first, and never dropping a `job_result` in favour of a `job_log` — a result is the answer to a signed job; a log line is commentary. Encode that as an explicit priority in the drop rule and test it: fill with logs, enqueue a result, overflow, assert the result survives;
- `Flush` is idempotent under a mid-flush disconnect: a message that was written but not acknowledged may be re-sent, which is fine — `health_report` and `security_events` are documented at-least-once, and `job_result` is idempotent server-side because `JobEngine._transition` makes terminal states terminal (`_ALLOWED[succeeded] == set()`); a duplicate result raises `InvalidTransition`, which the gateway logs and swallows (`gateway/main.py:212`). **Verify that claim against `engine.py` before relying on it** — it is the reason the agent is allowed to be at-least-once.

- [ ] **Step 3: Run to verify failure. Step 4: Implement. Step 5: Run to verify pass.**

- [ ] **Step 6: Wire into the runner and transport** — every outbound message goes through the buffer; the buffer flushes on connect before the runner processes anything new.

**Definition of done:** replay protection survives restart and is fail-closed at the cap with the spy-counter falsification pasted; the buffer's priority drop rule is tested; the at-least-once claim is verified against `engine.py` and the verification is quoted in the report.

Commit: `feat(agent): persisted replay store and bounded offline buffer`

---

## Task 9: The timer-driven scheduler and the idle-budget harness

**Files:**
- Create: `agent/internal/scheduler/scheduler.go`, `agent/internal/scheduler/scheduler_test.go`
- Create: `agent/internal/budget/budget.go`, `agent/internal/budget/budget_test.go`

**Interfaces:**
- Consumes: nothing agent-specific.
- Produces: `scheduler.Clock` (`Now() time.Time`, `NewTimer(d) Timer`), `scheduler.Real{}`, `scheduler.Fake{}` with `Advance(d)`; `scheduler.Wheel` with `Add(id string, every time.Duration, fn func(context.Context))`, `Remove(id string)`, `Run(ctx)`; `budget.Measure(name string, setup func() func()) budget.Result{AllocsPerOp, BytesPerOp int64, RSSDelta int64, Goroutines int}` and `budget.AssertNoAllocations(t, name, fn)`.

- [ ] **Step 1: Write the failing scheduler tests**

- N entries run on **one** goroutine, driven by a single timer set to the nearest deadline — assert `runtime.NumGoroutine()` is the same with 1 entry and with 32;
- with a `Fake` clock, advancing by `3 * interval` fires an entry exactly 3 times (not 1, not 4);
- `Remove` during a callback does not deadlock;
- **no polling:** with no entries due, advancing the fake clock by an hour performs zero callbacks and zero allocations.

- [ ] **Step 2: Implement the wheel** — a min-heap of deadlines, one `time.Timer` reset to the earliest, one goroutine. Not a `time.Ticker` per entry (that is a goroutine and a wakeup per entry) and never a polling loop.

- [ ] **Step 3: Build the budget harness**

The mechanism that makes `CLAUDE.md`'s requirement real:

```go
// Measure runs fn under testing.Benchmark and captures the numbers the idle
// budget is stated in. It is called from ordinary Test functions so a
// regression FAILS CI -- `go test -bench` alone exits 0 on a regression, which
// is how docs/modules/endpoint-health-alerting/spec.md §0.5's six falsifiers
// sat unrun for a phase.
func Measure(name string, setup func() func()) Result
```

`Result.RSSDelta` reads `/proc/self/statm` on Linux and falls back to `runtime.MemStats.HeapAlloc` after two `runtime.GC()` calls elsewhere. `Result.AllocsPerOp` comes from `testing.BenchmarkResult.AllocsPerOp()`.

- [ ] **Step 4: Assert the scheduler's own budget**

```go
func TestIdleSchedulerAllocatesNothing(t *testing.T) {
	// FALSIFIER: an idle wheel that allocates per wakeup accumulates, and this
	// reports AllocsPerOp > 0. Observation is the allocation count, not a
	// timing.
	r := budget.Measure("idle-wheel", func() func() { ... })
	if r.AllocsPerOp != 0 {
		t.Fatalf("idle wheel allocates %d per wakeup; the budget requires 0", r.AllocsPerOp)
	}
}
```

- [ ] **Step 5: Write down what is NOT asserted, and why**

In `agent/internal/budget/budget.go`'s package comment, and in the report:

> **`~0% CPU` is not asserted as a wall-clock percentage.** A shared GitHub runner cannot reproduce a CPU-time measurement tightly enough to be a gate — a flaky budget test gets disabled, and a disabled test is worse than an honest absence. What is asserted instead, and what each stands in for: **zero allocations on the idle path** (a busy loop allocates or, if it does not, is caught by the next two), **a constant goroutine count** (no spawned poller), **zero network writes on a quiet tick** (§0.5 falsifier 4), and **flat allocations across 100 vs 1000 ticks** (§0.5 falsifier 2 — the accumulation test). A wall-clock CPU number stays a design target and is described that way, per `CLAUDE.md`.

That paragraph is load-bearing: it is the difference between a measured claim and a claim dressed as one.

- [ ] **Step 6: Run** — `go test ./internal/scheduler/ ./internal/budget/ -count=1`.

**Definition of done:** the wheel is single-goroutine, timer-driven, and allocation-free at idle, proven by an assertion that fails CI; `budget.Measure` exists and is used; the "what is not asserted" paragraph is written.

Commit: `feat(agent): timer-wheel scheduler and an idle-budget harness that fails CI`

---

## Task 10: Capability registry and `inventory.collect`

**Files:**
- Create: `agent/internal/capabilities/registry.go`, `registry_test.go`
- Create: `agent/internal/capabilities/inventory/inventory.go`, `collect_linux.go`, `collect_windows.go`, `collect_darwin.go`, `inventory_test.go`

**Interfaces:**
- Consumes: `jobs.VerifiedJob`.
- Produces: `capabilities.Handler = func(context.Context, *jobs.VerifiedJob) (any, error)`; `capabilities.Register(jobType string, h Handler)`; `capabilities.Lookup(jobType string) (Handler, bool)`; `capabilities.Names() []string`; `inventory.Document` mirroring `InventoryDocument.schema.json`.

- [ ] **Step 1: Write the failing registry tests**

- `Lookup` of an unregistered type returns `false` (this is what makes Task 7's `unknown_type` refusal work);
- **dormancy:** registering a capability performs no I/O and allocates no per-capability state — `budget.Measure` over `capabilities.Register` for all handlers reports a bounded constant, and a spy capability's `init` counter stays 0 until `Lookup` + invoke. Falsifying observation: a capability that opens a socket or spawns a goroutine at registration shows up as a changed goroutine count and a non-zero setup counter.

- [ ] **Step 2: Write the failing inventory test**

A golden-document test: run the collector against a fake OS-facts provider and assert the emitted JSON validates against `docs/kernel/wire-protocol/InventoryDocument.schema.json` (hand-rolled validation of `required` + `pattern` for `os_family` and `components[].type` — no JSON-schema dependency), and that `os_family` is one of `windows|linux|macos`.

- [ ] **Step 3: Run to verify failure. Step 4: Implement.**

Per-OS collectors behind build tags. Linux reads `/proc` and `/sys` and `dmidecode`-free paths where possible; Windows uses WMI via `wmic`-free COM or `powershell` shell-outs (documented, and off the idle path — this capability only runs inside a job); macOS uses `system_profiler`/`sysctl`. Keep the exec surface small and never pass job-supplied strings to a shell (`InventoryCollectParams` is empty, so there is nothing to pass — assert that in a test so it stays true).

**Result shape:** the handler returns the `Document` itself. `handle_result` does `InventoryDocument(**result)` on the top-level result dict (`inventory/handler.py:27`) — do **not** nest it under a `document` key.

- [ ] **Step 5: Run the demo** — `inventory.collect` must now succeed and the demo's device-hostname read must show a real hostname. Paste it.

- [ ] **Step 6: Budget assertion** — `budget.AssertNoAllocations` over the registry at rest; assert the inventory package's collectors are not touched until a job arrives.

**Definition of done:** registry dormancy proven by a counter; the inventory document validates against the exported schema; `scripts/demo_e2e_agent.py` passes end to end for the first time.

Commit: `feat(agent): capability registry and the inventory.collect collector`

---

## Task 11: The NIC address block (IPAM task 13) — and the compatibility trap in it

**Files:**
- Modify: `agent/internal/capabilities/inventory/*.go`; Test: `agent/internal/capabilities/inventory/nic_test.go`
- Modify (control plane): `backend/app/modules/ipam/nic.py`; Test: `backend/tests/test_ipam_nic.py`
- Regenerate: `docs/kernel/wire-protocol/InventoryDocument.schema.json` (no change expected — see below)

**Read `docs/modules/ipam/spec.md` §14.1 and `app/modules/ipam/nic.py` before starting.**

Today `nic.py` reads exactly one key: `attributes["ip"]` — a bare address, no prefix length, one address per NIC, no origin. §14.1 specifies the replacement:

```jsonc
{ "type": "nic", "identifier": "a4:bb:6d:11:22:33",
  "attributes": {
    "name": "Ethernet0",
    "mac": "a4:bb:6d:11:22:33",
    "addresses": [
      {"address": "10.1.2.42/24", "family": "ipv4", "origin": "dhcp"},
      {"address": "fe80::1%4/64", "family": "ipv6", "origin": "link_local"}
    ]
  } }
```

`origin ∈ {static, dhcp, slaac, link_local, unknown}`.

**The trap, stated plainly.** §14.1's compatibility analysis covers only *old agent → new server* ("`InventoryDocument` is `extra="ignore"`, so an older agent that omits the block still validates; IPAM falls back to the legacy `ip` key"). The other direction is the one that bites here: `nic.py` reads **only** `ip`, so a new agent that emits `addresses[]` and drops `ip` produces **zero** observations against every control plane that has not yet learned the new key — the entire passive reconciliation stream goes silent, and nothing goes red, because IPAM's parser is total by design and returns `[]` rather than raising. `docs/kernel/wire-protocol/README.md`'s rule applies exactly: **loosen inbound before you tighten outbound.**

- [ ] **Step 1: Control plane first — teach `nic.py` to read `addresses[]`**

Write the failing test in `backend/tests/test_ipam_nic.py` before touching `nic.py`:

- a NIC with only `addresses[]` yields one `NicObservation` per documentable address, with the **canonical host address** (the parser already renders canonical text) and `mac` from `attributes.mac` when present, falling back to `identifier` when that parses as a MAC;
- a NIC with **both** `ip` and `addresses[]` yields the `addresses[]` set and does **not** double-count the address that also appears in `ip` (the existing `seen` dedup key is `(address, mac)`, so this should hold — assert it rather than assume it);
- a NIC with only `ip` still yields exactly what it yields today (the legacy path is not removed; add a test asserting the existing behaviour is unchanged);
- **totality is preserved**: `addresses` that is not a list, an entry that is not a dict, a missing `address`, an unparseable address, an `origin` outside the enum, a 10,000-entry list — each degrades to "no observation for that entry" and **never raises**. `test_nic_module_imports_no_database` and the "may not raise, for any input, ever" contract in `nic.py`'s docstring are the constraints; a raising parser fails the whole `inventory.collect` job (`JobEngine.complete` converts a raising result handler to `failed`), which means IPAM would break Phase 1 inventory for that device.
- bound the address list per NIC (say 64) so a hostile or broken agent cannot make one NIC cost the ingest path unbounded work.

Then implement in `nic.py`: try `addresses[]` first, fall back to `ip`. `NicObservation` gains nothing — `origin` is consumed by §3.2's `allocation_method`, which is a separate IPAM change **outside this plan's scope**; note in the report that the agent now supplies `origin` and IPAM does not yet read it, so §3.2's `allocation_method` stays `unknown` until IPAM does. Do not add that plumbing here; it is IPAM work with its own tests.

- [ ] **Step 2: Prove the control-plane test can fail** — delete the `addresses[]` branch, watch the new tests report zero observations, restore. Paste it.

- [ ] **Step 3: Now the agent — write the failing NIC test**

Per-OS interface enumeration through `net.Interfaces()`/`net.InterfaceAddrs()` (stdlib, cross-platform, gives prefix length for free), with an OS-specific `origin` prober. Assert:
- an interface with two IPv4 addresses emits two `addresses[]` entries, each with its prefix length;
- `family` is `ipv4`/`ipv6` derived from the parsed address, not from a string search for `:`;
- `origin` is one of the five values and defaults to `unknown` when the OS cannot say — never invents `static`;
- loopback interfaces are included in the document (the server discards undocumentable addresses in `net.is_documentable`; the agent does not duplicate that judgement — one implementation of "documentable", and it is the server's).

- [ ] **Step 4: Emit both keys**

The agent writes `attributes.addresses[]` **and** keeps `attributes.ip` set to the first documentable IPv4 address. This is the "loosen inbound before tighten outbound" rule made concrete: a new agent must work against an old control plane. Add the removal to `docs/modules/agent/open-questions.md` as **OQ-2**, with the condition for removal stated: `ip` may be dropped only after the control-plane change in Step 1 has been deployed everywhere and the fleet's minimum agent version is past this release. Write a test asserting **both** keys are present, with a docstring explaining that deleting `ip` is a fleet-compatibility decision and not a cleanup.

- [ ] **Step 5: `origin` probers**

Linux: `/proc/net/route` + `ip -j addr` when present, else `unknown`; IPv6 SLAAC detected from the address having no matching DHCPv6 lease and being globally scoped with a `/64` — if that is not determinable, `unknown`. Windows: `Get-NetIPAddress`'s `PrefixOrigin`/`SuffixOrigin` mapped explicitly. macOS: `ipconfig getpacket <if>` for DHCP, else `unknown`. **Every mapping is explicit and every unmapped case is `unknown`.** A confidently wrong `static` is worse than an honest `unknown`, exactly as §4.1 argues for `posture.antivirus_healthy`.

- [ ] **Step 6: Regenerate and inspect the exported schema**

`backend/.venv/bin/python scripts/export_wire_schema.py`. Expect **no diff**: `ComponentDoc.attributes` is `dict[str, Any]` / `additionalProperties: true`, so the block is inside a field the schema does not describe. Say so in the report — a wire change that produces no schema diff is precisely the kind the export guard cannot catch, and the compatibility analysis in Step 4 is the only control on it.

**Compatibility:** *old agent → new server*: legacy `ip` still parsed, fallback intact, tested. *New agent → old server*: `addresses[]` ignored, `ip` still emitted, stream unbroken — this is why Step 4 exists. *Deployment order*: control plane first (Step 1), agent second.

**Definition of done:** `nic.py` reads both shapes with totality preserved and the falsification pasted; the agent emits both keys with a test pinning that; `origin` never guesses; the no-schema-diff observation is reported; OQ-2 written.

Commit: `feat(agent): NIC address block with prefix length and origin; feat(ipam): parse attributes.addresses[]`

---

## Task 12: `health.check` — the edge tick, and the six falsifiers

**Files:**
- Create: `agent/internal/capabilities/health/policy.go`, `checks.go`, `tick.go`, `report.go`, and `_test.go` for each
- Create: `agent/internal/capabilities/health/budget_test.go`

**Read `docs/modules/endpoint-health-alerting/spec.md` §0.5, §0.7, §4.1, §4.2, §4.3, §5.1, §5.2, §5.3.**

**Interfaces:**
- Consumes: `jobs.VerifiedJob`, `scheduler.Wheel`, `transport`, `budget`.
- Produces: handlers for `health.policy.apply` and `health.check.now`; `health.State` — the per-check struct.

- [ ] **Step 1: Write the failing policy-install tests**

`health.policy.apply` params come from `HealthPolicyApplyParams.schema.json`: `policy_version int`, `tick_interval_s 60..3600`, `posture_interval_s >= 900`, `summary_interval_s 300..86400`, `checks []HealthCheckSpec` (`max 32`), each `{check_id, check_type, selector, comparator ∈ gt|gte|lt|lte|eq|neq|is, threshold string, for_ticks 1..60, cadence ∈ tick|posture}`.

- installing a policy replaces the previous one wholesale and adds **exactly one** entry to the timer wheel (assert `wheel.Len() == 1` after installing a 32-check policy — §0.5's mechanism);
- **an empty `checks` list is a legal policy meaning "run nothing"** — it disarms. Assert that after installing `checks: []` at `policy_version: 0`, subsequent ticks emit nothing and the previous policy's checks are gone. Falsifying observation: a check from the prior policy still emitting;
- a policy exceeding the caps (33 checks, `tick_interval_s: 30`, `for_ticks: 61`) is **refused** — §0.5 falsifier 6. The bounds live in the params model server-side; duplicating them agent-side is deliberate (`jobtypes.py`'s docstring says so: "a bound that lives only in the database does not travel with the job"). Falsifying observation: the spy counter shows the tick engine armed with 33 checks;
- `check_id` is echoed **verbatim**. It is `str(check.id)` server-side (`policy_push.py:115`) — a UUID string. The agent never parses it, never normalises it, and never generates one.

- [ ] **Step 2: Write the failing tick tests (§4.2)**

Driven by the `Fake` clock and a fake sampler:

- a check that compares true for `for_ticks - 1` ticks emits **nothing**; on tick `for_ticks` it emits exactly one `breach` line with `state: "breaching"`;
- once breaching, a single non-matching sample emits one `recovery` with `state: "ok"` — recovery is **not** duration-gated (the asymmetry is deliberate);
- a tick with no transition and no summary due produces **zero** frames on the fake transport — §0.5 falsifier 4, asserted as a write counter;
- `summary_interval_s` produces one message carrying every applicable check's current state and the installed `policy_version`;
- posture-cadence checks fire only on the posture sub-tick: run `posture_interval_s / tick_interval_s - 1` fast ticks and assert the posture prober's call counter is **0** — §0.5 falsifier 3;
- `unavailable` is a real third state: a sampler returning "cannot answer" produces `state: "unavailable"` and **never** `breaching`, and does not advance the consecutive counter;
- `eventlog.*` check types report `unavailable` in v1, because the security-logs capability is not built. §0.5 falsifier 5 and §4.1†: they must **not** open their own OS log subscription. Assert the subscription-factory counter is **0** — that is falsifier 5 as an observation, not a promise;
- `agent.offline` is never present in a pushed policy (the server filters it), but if one arrives the agent ignores it rather than inventing an evaluation — assert it is dropped with a WARN;
- **value rendering** is pinned: numeric percentages as `strconv.FormatFloat(v, 'f', 1, 64)` (`"3.1"`, matching §5.1's example), byte counts and event counts as base-10 integers, booleans as `"true"`/`"false"`, enum checks as the **member name** (`"running"`) — §4.1's amendment says the registry emits the member name on the wire, so the agent compares like against like. `value` is `max_length=64`; truncate and log rather than emit an over-length line that the server drops whole.

- [ ] **Step 3: Write the failing report tests (§5.1)**

- `reports` is bounded `1..64`; an agent with more to say sends **more messages**, never one over-long one. Assert a 40-check policy emitting a summary plus 30 edges produces two messages, and that no message is ever sent with `reports: []` (`min_length=1` — an empty array is rejected whole by `parse_agent_message` and the failure is a silent server-side warning);
- the message carries **no device field**. Assert the encoded key set is exactly `{v, kind, policy_version, reports}` — `_Msg` is `extra="forbid"`, and identity comes from the mTLS binding (§5.1, `protocol.py:124-141`);
- `policy_version` is the version the agent believes it is running, and it is `>= 0`. Assert that after a disarm it is `0`. **This is load-bearing:** `ingest.py:552` discards a report whose `policy_version < acknowledged - 1`, so an agent that lies or lags by two versions has its breaches silently dropped;
- no sample arrays, no window, no rolling average anywhere in the encoder — assert the per-check state struct has exactly the fields §0.5 names (`consecutive int32`, `state uint8`, `last float32`) via a `unsafe.Sizeof`/reflect field-count test. Falsifying observation: a struct that grew a `[]float64`.

- [ ] **Step 4: Run to verify failure. Step 5: Implement.**

Per check: `struct{ consecutive int32; state uint8; last float32 }` — 12 bytes, and the whole policy is one slice allocated at install. Samplers write into a reused buffer. One wheel entry; the posture sub-tick is a counter on the fast tick, not a second entry.

- [ ] **Step 6: The six falsifiers as CI assertions**

`agent/internal/capabilities/health/budget_test.go`, one test per falsifier, each named for it:

| # | §0.5 falsifier | Assertion |
|---|---|---|
| 1 | RSS delta above 2 MB on the default profile | `budget.Measure` around installing a 12-check policy and running 1000 ticks; `RSSDelta < 2<<20` |
| 2 | per-tick allocation proportional to elapsed ticks | run 100 and 1000 ticks; `allocs(1000) <= allocs(100) * 1.1` **and** steady-state `AllocsPerOp == 0` on the quiet path |
| 3 | posture probes on the fast tick | posture prober call counter is 0 across `posture_interval_s/tick_interval_s - 1` fast ticks |
| 4 | a network write on a quiet tick | fake transport write counter is 0 across 1000 quiet ticks |
| 5 | an eventlog check opening its own subscription | subscription-factory counter is 0; every `eventlog.*` check reports `unavailable` |
| 6 | a profile permitted to exceed the caps | a 33-check / out-of-range policy is refused and the tick engine is not armed |

Each test's docstring names the falsifier and the observation. **Then delete the guard for at least falsifiers 2, 3 and 4 in turn, watch each go red, restore, and paste all three.** A budget test that has never been observed red is the thing §0.5's amendment is complaining about.

- [ ] **Step 7: End-to-end against a real control plane**

Extend `scripts/demo_e2e_agent.py` (Task 15 owns the file; do the temporary local version here) to create a health profile, assign it, and assert a `health_report` lands and `device_health_policy_ack.policy_version` advances. Paste it.

**Definition of done:** all tick, policy and report tests green; all six falsifiers asserted from ordinary `Test` functions that fail CI; three of them observed red; §0.5's numbers may now be described as **measured** for the first time — say so explicitly in the report, and update `docs/modules/endpoint-health-alerting/spec.md` §0.5's "Amendment" paragraph in Task 15.

Commit: `feat(agent): health.check -- edge tick, policy install, and the six falsifiers as CI assertions`

---

## Task 13: `net.sweep` — job-scoped, dormant, and unable to widen its own scope

**Files:**
- Create: `agent/internal/capabilities/netsweep/netsweep.go`, `probe.go`, `digest.go`, and `_test.go` for each
- Create (control plane): `scripts/export_scope_digest_corpus.py`, `backend/tests/test_scope_digest_corpus.py`

**Read `docs/modules/ipam/spec.md` §5.2, §5.4, §5.5, §6.2, §6.4, and C6 above.**

**Interfaces:**
- Consumes: `jobs.VerifiedJob`.
- Produces: handler for `net.sweep`; `netsweep.Digest(targets []string) string`.

- [ ] **Step 1: `scope_digest` — one *rule*, two languages, pinned by a corpus (C6)**

`scripts/export_scope_digest_corpus.py` writes `agent/internal/capabilities/netsweep/testdata/scope_digest.jsonl` from `app.modules.ipam.net.scope_digest`: `{"targets": [...], "digest": "<hex>"}`. Cases: empty list, one CIDR, many, IPv6, mixed families, a list whose order is **not** sorted (the Python function deliberately does not sort — hashing the list as given is the point), and a pair `["a","b"]` vs `["a,b"]` proving the JSON encoding is what stops the collision.

`backend/tests/test_scope_digest_corpus.py` keeps it current, with the same docstring pattern as Task 3's.

`netsweep.Digest` is `sha256(json.dumps(targets, separators=(",",":")))` — reuse `canonical` for the string escaping (a CIDR string is ASCII, but the encoder is already correct and a second string escaper is a second thing to get wrong). Pin it against the corpus.

**What "one implementation" means for the agent**, per C6: the agent hashes `params.targets` **exactly as received** — no sorting, no `collapse_addresses`, no re-rendering, no normalisation. If the agent ever derives its own list and hashes that, the digest can agree by luck and the authorized-scope check silently stops matching what was authorized. Write that as a comment on `Digest` and as a test: a target list in non-canonical order must produce the digest of *that* order, and the test asserts it differs from the digest of the sorted order.

- [ ] **Step 2: Write the failing scope tests**

- **the agent verifies `Digest(params.targets) == params.scope_digest` before opening a socket**, and refuses the job (`job_result{state:"refused"}`) if not. §6.4 has the server reject an out-of-scope result whole; this is the agent side that keeps it from *accidentally* producing one, and it is strictly earlier — a mismatch means the params were mangled in transit or the agent's digest is broken, and either way no packet should leave the host. **Falsifying observation:** with the check removed, the probe counter is non-zero for a job whose digest does not match. Assert the probe counter, not the returned error;
- **every probed address is contained by `params.targets`** — property test: generate target lists, run the sweep against a fake prober, assert `∀ probed ∈ ⋃ targets`. Falsifying observation: a probed address outside the union, printed;
- `hosts` never contains a duplicate address. `handle_sweep_result` rejects the **whole** document if it does (`handler.py:380-393`, the `repeated` branch) — the agent dedupes by address before emitting so a legitimate sweep is never thrown away for it;
- `len(hosts) <= min(params.max_targets, total addresses in params.targets)`. That is the server's exact budget (`handler.py:308-311`); computing it the same way agent-side means the agent stops rather than produces a document the server discards;
- `subnet_id`, `sweep_run_id`, `scope_digest` are **echoed verbatim** from params. Assert byte equality against the input strings, not a re-parse-and-re-render (the server compares canonical UUID text; a re-render is a chance to differ);
- `NetSweepDocument` carries **no RTT, no per-probe timing, no retry count, and no `reachable:false` entry**. `SweepHost` is `extra="forbid"` server-side. Assert the encoded per-host key set is exactly `{address, responded_via}` plus `mac`/`hostname` when non-null — an extra key means the whole document is rejected by Pydantic and the job fails.

- [ ] **Step 3: Write the failing idle/dormancy tests (§5.5)**

- **Dormant by default:** no socket, no goroutine, no buffer, and no allocation before a job arrives. `budget.Measure` with the capability registered and idle reports the same numbers as with it absent. Falsifying observation: a non-zero allocation delta or a changed goroutine count attributable to the package;
- **released after the job:** goroutine count and open file descriptors return to their pre-job values within the job's teardown (read `/proc/self/fd` on Linux). Falsifying observation: a leaked fd, named;
- **no timer-wheel entry** — assert `wheel.Len()` is unchanged by installing the capability and by running a sweep. §0.9: the cadence is server-side, and a locally scheduled sweep is a standing authorization executed for weeks after the scope was revoked;
- **the rate limit and budget are honoured**: with `rate_pps: 10` and 100 targets on a fake clock, at most 10 probes occur per simulated second; hitting `budget_s` sets `truncated: true` and stops rather than running longer.

- [ ] **Step 4: Run to verify failure. Step 5: Implement.**

`golang.org/x/net/icmp` for the echo probes, `ipv4`/`ipv6` for TTL/hop-limit. ARP on Linux via `AF_PACKET` behind a build tag; on Windows and macOS, `method: "icmp+arp"` degrades to ICMP-only and the ARP half is reported in `errors[]` (bounded to 20 entries, 200 chars each) rather than failing the job. Raw sockets are opened at job start and closed at job end, in a `defer`, and the AST walker from Task 4 gets its **one** documented allowance here — with a comment stating that a raw socket receiving replies to probes this process sent is not a listening service, and a test asserting no *bound TCP or UDP port* appears (reusing Task 4's `listeningPorts`).

If the process lacks the privilege to open a raw socket, the capability **refuses the job** with `reason: "net.sweep requires raw socket privilege"` rather than falling back to a TCP connect scan. A silent method substitution would put different packets on someone else's wire than the ones the server authorized.

`hostname` is best-effort reverse DNS with a short timeout, and a failure yields `null` — it is display-only and never a reconciliation predicate.

- [ ] **Step 6: Falsify the scope guard**

Delete the `Digest(params.targets) == params.scope_digest` check, run the suite, confirm the probe-counter assertion goes red. Then delete the containment filter, confirm the property test reports a probed address outside the union. Restore both. Paste both.

- [ ] **Step 7: Report the capability**

The agent's `capabilities` advertisement (Task 5) now includes `net.sweep`, which is what makes IPAM's preferred dispatch path (`spec.md` §5.3 step 1) work rather than relying on the `refused` fallback.

**Definition of done:** the digest corpus is green in both languages and observed red once; the containment property test and the probe-counter falsifications are pasted; dormancy and fd-release are asserted; no timer-wheel entry; the AST-walker allowance is documented and the no-bound-port test is green.

Commit: `feat(agent): net.sweep -- job-scoped discovery bounded by its signed scope`

---

## Task 14: Packaging and service integration

**Files:**
- Create: `agent/packaging/linux/its-agent.service`, `agent/packaging/linux/build-deb.sh`, `agent/packaging/linux/build-rpm.sh`
- Create: `agent/packaging/windows/service.go` (build-tagged), `agent/packaging/windows/its-agent.wxs`
- Create: `agent/packaging/darwin/com.toeshee.itsagent.plist`, `agent/packaging/darwin/build-pkg.sh`
- Create: `agent/packaging/README.md`
- Modify: `.github/workflows/ci.yml` (artifact upload on tag)

- [ ] **Step 1: systemd unit** — `Type=notify` is tempting but needs a dependency; use `Type=simple` with `Restart=always`, `RestartSec=5`, `User=root`, and hardening directives that do **not** break raw sockets (`AmbientCapabilities=CAP_NET_RAW`, `NoNewPrivileges=false` — document why each hardening flag is or is not set). `Environment=ITS_AGENT_DATA_DIR=/var/lib/its-agent`.

- [ ] **Step 2: Windows service** — `golang.org/x/sys/windows/svc`. This is a **third** dependency; justify it in the commit (`x/sys` is effectively stdlib-adjacent and there is no reasonable alternative for SCM integration) or shell out to `sc.exe` from the MSI and keep the binary a plain console app. Prefer the latter if it works — fewer dependencies is the standing instruction.

- [ ] **Step 3: launchd plist** — `RunAtLoad`, `KeepAlive`, `StandardErrorPath` under `/var/log/its-agent/`.

- [ ] **Step 4: Installer parameters** — every packaging path accepts the enrollment token and control-plane URL as install-time parameters and writes them to the service environment, per `docs/05-agent-spec.md`'s zero-touch requirement. **The token must not land in a world-readable file or in a process command line** — write it to a `0600` file in the data dir that `config.Load` reads, and delete it after a successful enrollment. Test that deletion.

- [x] **Step 5: Code signing** — documented, not implemented. *(`packaging/README.md`'s "Code signing — documented, not implemented" section, and **OQ-3 written 2026-08-06** in `docs/modules/agent/open-questions.md`, in OQ-1/OQ-2's shape: the facts as a sourced table, the risk, the gate — four things needed from outside this repository, three of them procurement — and what a decision would have to answer. The entry's load-bearing paragraph is why the obvious shortcut is worse than the gap: an agent that downloads a binary over the already-mTLS channel and checks a control-plane-supplied hash lets a compromised control plane replace root/SYSTEM code on every endpoint while vouching for itself.)* Signing keys are not in this repo and must not be (`CLAUDE.md`: secrets never in code or git). `packaging/README.md` states where each platform's signing step slots in and what the CI secret names would be. Note that `docs/05-agent-spec.md`'s self-update section depends on code signing and that **self-update is not built by this plan** — add it as **OQ-3**.

- [ ] **Step 6: CI artifacts** — extend the `agent` job to upload the six cross-compiled binaries on a tag push. Keep the per-PR job build-only.

**Definition of done:** all three service definitions present and documented; the enrollment token never appears in a command line or a world-readable file, with a test; signing and self-update are written up as gaps rather than half-built.

Commit: `feat(agent): packaging for windows, linux, and macos`

---

## Task 15: Close the loop — the demo, and the docs that still say the agent is elsewhere

**Files:**
- Modify (control plane): `scripts/demo_e2e_agent.py`, `scripts/export_wire_schema.py` (docstring), `backend/app/modules/ipam/schema.py` (docstring), `backend/app/modules/health/jobtypes.py` (docstring)
- Modify (docs): `docs/modules/ipam/spec.md` §5.1/§5.5/§14.1, `docs/modules/endpoint-health-alerting/spec.md` §0.5
- Create: `docs/modules/agent/README.md`

- [x] **Step 1: Check what the concurrent sweep already did**

`git diff docs/05-agent-spec.md docs/kernel/wire-protocol/README.md` — at plan time both are being rewritten by another agent to say the agent lives under `agent/`. **Do not redo that work.** `grep -rn "it-suite-agent" --include='*.py' --include='*.md' .` and fix only what remains.

> **Done 2026-08-06, and the answer was mostly "nothing".** Every remaining
> mention in `docs/**` is already correctly PAST-TENSE and names `CLAUDE.md`
> commit `a32cead` as the reversal — `05-agent-spec.md`,
> `kernel/wire-protocol/README.md`, `01-architecture.md`, `kernel/spec.md`,
> `06-roadmap.md`, `agent/README.md`, and the security-logs / backup / ipam /
> endpoint-health-alerting / site-server specs. Rewording a correct historical
> sentence is not a fix.
>
> **What remains is thirteen BACKEND docstrings**, listed by path in
> `docs/modules/agent/README.md`. They are comments, so nothing behaves
> incorrectly; what they cost is one specific wrong conclusion in the next
> reader's head — "the Go capability for this lives in another repo, so it is
> not my problem." Left for whoever owns `backend/**` next, because editing
> thirteen files concurrently held by other sessions to change a comment is a
> merge conflict, not a fix.
>
> Two staler things were found and fixed instead, both worse than the
> repository claim because both were about CAPABILITY rather than location:
> `kernel/wire-protocol/README.md` called `net.sweep` "unbuilt" (it has been
> built since Task 13) and `05-agent-spec.md` said "**Nothing in this
> repository checks any of that**" about the idle budget (CI has run an `agent`
> job with a Go toolchain since `0203612`, and the falsifiers are now measured).

- [x] **Step 2: Point the demo at the in-tree build** — *done 2026-08-06;
`ITS_AGENT_BIN` now defaults to `agent/dist/its-agent`, `scripts/build_agent.sh`
added and named in the not-found message, both docstrings rewritten.*

`scripts/demo_e2e_agent.py`'s `ITS_AGENT_BIN` default becomes `agent/dist/its-agent` relative to the repo root, and its docstring stops calling the agent a separate repository. Add a `make agent` / `scripts/build_agent.sh` one-liner it names in its error message.

- [ ] **Step 3: Extend the demo to cover all three owed capabilities**

After the existing `hello` + `inventory.collect` steps, add: a health profile + assignment producing a `health_report` and an advancing ack; and a `net.sweep` with an authorization producing `ipam_observation` rows. Assert the audit chain still verifies at the end (the demo already does this — keep it last).

- [ ] **Step 4: Update the two specs' accountability paragraphs**

`docs/modules/endpoint-health-alerting/spec.md` §0.5's "Amendment — where these falsifiers are checked, stated plainly" currently says *"Nowhere in this repository."* After Task 12 that is false. Replace it with what is now true, per falsifier, naming the test file and function — and keep the paragraph's honesty: whatever is still unasserted (the wall-clock CPU percentage, per Task 9 Step 5) stays described as a design target.

`docs/modules/ipam/spec.md` §5.1, §5.5 and §14.1 each say "No agent code is written in this repo" / "that work belongs in that repo". Rewrite them to point at `agent/internal/capabilities/netsweep/` and `agent/internal/capabilities/inventory/`, and carry §14.1's compatibility note forward with Task 11's *new-agent-to-old-server* direction added — it is missing today and it is the one that bites.

- [x] **Step 5: Write `docs/modules/agent/README.md`** — build, run, enroll, test, and the three open questions (OQ-1 renewal, OQ-2 the legacy `ip` key, OQ-3 self-update), each with its blocking condition. **Done 2026-08-06.** It also carries the list of thirteen backend docstrings that still call the agent a separate repository, by path — see Step 1's amendment.

- [ ] **Step 6: Run everything** — `cd agent && go test ./... -count=1`; `cd backend && .venv/bin/pytest -q`; `.venv/bin/mypy app`; `.venv/bin/ruff check app tests`; `scripts/demo_e2e_agent.py`. Paste all five.

**Definition of done:** no doc or docstring in the repo claims the agent lives elsewhere; the demo exercises all three owed capabilities; §0.5's amendment tells the truth about what is now measured and what still is not.

Commit: `docs(agent): the agent is in-tree, its budget is measured, and three gaps are named`

---

## Self-review

**Coverage.** Foundation (scaffold 1, wire 2, canonical 3, transport 4, enrollment 5, cert lifecycle 6, signed-job runner 7, replay/buffer 8, scheduler 9) precedes the three owed capabilities (inventory 10, NIC block 11, health.check 12, net.sweep 13), then packaging 14 and the doc/demo close 15. `docs/modules/endpoint-health-alerting/spec.md` §0.5's six falsifiers each have a numbered assertion in Task 12 Step 6. §2.2's builtin registry and the `health_report` wire kind are Tasks 12 Steps 2-3. IPAM §5 and §6 are Task 13. §14.1 is Task 11.

**Not covered, deliberately, and named:** certificate renewal (OQ-1, blocking), self-update (OQ-3), the security-logs capability and therefore working `eventlog.*` checks (they report `unavailable`, which is spec-compliant per §0.5 item 5), `patch.*`, `backup.*`, `remote.session.open`, `app.deploy`, `script.run`, `provision.*`, **`site.wake`** — every one of those has an exported schema and no handler, and Task 2 Step 2 forces each into an explicit `notImplemented` list with a reason rather than letting them be forgotten.

> **BUILT 2026-08-06.** The note below was added earlier the same day and is
> kept because it is the record of an obligation being tracked rather than
> forgotten, which is the whole point of writing one down. What it describes
> now exists: `agent/internal/capabilities/site/`, registered in
> `capabilities/registry.go`, with `schema_test.go` asserting the Go structs
> against the two exported schema files and `budget_test.go` measuring both of
> §5.4's falsifiers. `SiteWakeParams`/`SiteWakeResult` moved out of Task 2's
> `notImplemented` set into that package's own guard.
>
> Two things the note got right and one it did not anticipate. Right: the port
> bound had to be checked on the endpoint, and `broadcast` had to be refused if
> it carried a prefix length. Not anticipated: the socket is **dialled**, not
> bound — `net.Dialer` with a Control hook setting `SO_BROADCAST` — so the
> capability needed **no allowance** in
> `internal/transport/listen_test.go`, unlike `net.sweep`. The obvious
> `net.ListenPacket` implementation would have needed one, and it would have
> been an exemption bought for a socket that receives nothing.
>
> The original note follows.
>
> **Added 2026-08-06 — `site.wake` is an obligation this plan does not yet
> carry.** The site-server module shipped its control-plane half
> (`docs/modules/site-server/spec.md` §5.4) and exported
> `SiteWakeParams.schema.json` and `SiteWakeResult.schema.json`. **No Go code
> implements it**, and this plan does not schedule it — it plans
> `inventory.collect`, `health.check` and `net.sweep`. Recorded here so it is
> "not done yet" rather than "forgotten"; it belongs in Task 2's
> `notImplemented` set with a reason until a task exists for it.
>
> What the capability would be: open a UDP socket, set `SO_BROADCAST`, send the
> magic packet to **exactly** the `broadcast` address in the signed params
> (never a re-derived one — the control plane decides what the endpoint may
> touch), repeat 1–3 times ~200 ms apart, close. Report `{sent, broadcast,
> sent_at}` and nothing else: there is no `woke` field because a magic packet
> is unacknowledged, and the server rejects a result whose `broadcast` differs
> from what it signed.
>
> **Its idle budget is a design target, not a measured result, and must be
> described that way.** Spec §5.4 states the falsifiers — a socket or
> timer-wheel entry resident on an agent with no wake job in flight, and
> resident memory that does not return to its pre-job baseline after one — and
> states no benchmark, because none exists. `CLAUDE.md` forbids reporting a
> footprint number as verified until a benchmark runs **and** a falsifier is
> stated. The `agent` CI job compiles and tests what exists, which for this
> capability is nothing; a green build there is not agent-side verification.


**Consistency check performed:** `VerifiedJob` is the handler argument in Tasks 7, 10, 12 and 13; `budget.Measure` has one signature, used in Tasks 9, 10, 12 and 13; `scheduler.Wheel.Add/Remove/Len` is consistent between Tasks 9, 12 and 13; the corpus mechanism in Tasks 3 and 13 is the same shape (`scripts/export_*_corpus.py` + a `backend/tests/test_*_corpus.py` currency guard + a Go `testdata` file).

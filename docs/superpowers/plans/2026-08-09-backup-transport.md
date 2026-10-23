# Backup Transport Implementation Plan

> **OUTCOME (recorded after the fact, do not edit the plan below to match).**
> This plan's stated Goal -- "make `backup.run` complete end to end" -- was
> **NOT achieved**. Every task here shipped, and a later live measurement
> against real MinIO then found `backup.run` still refuses in a stock binary:
> `main.go` never sets `Options.OpenSnapshot`, and the dedup exchange
> deadlocks against the agent's own single-reader transport. See
> `docs/modules/backup/spec.md` §6.1 items 4-5 and `HANDOFF.md` §2. This plan
> is kept as a checked-in record of what was built, not as a statement that
> the Goal was met.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `backup.run` complete end to end by giving the agent the wire framing, the reply correlation and the POST-grant object store it is missing.

**Architecture:** The control plane already answers the dedup exchange and mints a prefix-scoped S3 POST grant; the endpoint cannot speak it. This plan adds the two message kinds to `internal/wire`, a small `chunkreply` registry that correlates a grant back to the flush that asked for it, a `backupstore` that spends a grant over plain HTTPS, and the `main.go` wiring that connects them. The server learns to derive `manifest_ref` from the suffix the agent reports.

**Tech Stack:** Go 1.24 (agent, stdlib only — no new dependencies), Python 3.12 / FastAPI / SQLAlchemy 2.x (control plane), MinIO + Traefik (verification).

**Spec:** `docs/superpowers/specs/2026-08-09-backup-transport-design.md`. Where this plan and the spec disagree, the spec wins — raise it rather than resolving it silently.

## Global Constraints

Every task's requirements implicitly include this section.

1. **No new Go dependency.** The agent is a single static binary with minimal dependencies. Everything here is stdlib (`net/http`, `mime/multipart`, `crypto/tls`, `sync`). If a task seems to need a library, stop and raise it.
2. **Invariant #3 — no credential on the endpoint.** The POST grant is a short-lived bearer credential scoped to one prefix. It is never logged, never placed in a job result, never persisted, and dies with the job.
3. **Invariant #1 — signed jobs only.** Nothing in this plan adds a command path. `backup_chunk_grant` carries no instruction: it carries a dedup answer and a write grant, and it is only ever consumed by a flush that is already running under a verified job.
4. **The agent idle budget is a hard requirement.** No goroutine, no allocation, no file handle and no socket may be created by any of this while no backup job is running.
5. **Outbound is exact, inbound is loose** (`wire.go` package comment). The server is `extra="forbid"`: one undeclared key and the whole message is dropped with only a `logger.warning`. Inbound, unknown fields are ignored so an additive server change does not take the fleet offline.
6. **`MAX_CHUNK_BATCH` is 1024** on both sides (`protocol.py:181`, `dedup.go:60`). Pinned to the exported schema's `maxItems`, never to a comment.
7. **Never edit a shipped migration.** This plan adds none; if one becomes necessary, stop and raise it.
8. **Tests first, always.** Every task writes a failing test, runs it to watch it fail, then implements. A test that has never been observed failing is not evidence.
9. **Assert the state of the world, not "did not raise".** A guard test whose fixture makes the guarded and unguarded paths produce the same observable is this project's defining defect. Assert tuples.
10. **When demonstrating a falsifying mutation, verify the mutation applied.** A find-and-replace that matches nothing passes and reads as proof. Confirm the edited text actually changed before running.
11. **Commit after every green step, and push immediately.** This container has rolled back its filesystem mid-session, wiping the local git object store. Committed-and-pushed work survived; uncommitted work did not.
12. **Never `git add -A`, `git add .`, `git commit -a`, `git checkout .`, or `git stash`.** Stage explicit paths only. Run a bare `git reset` (mixed, no pathspec) before staging.
13. **Branch:** `claude/handoff-spec-review-f45idd`. Do not push anywhere else.
14. **Baseline before you start.** `cd agent && go test ./... -race -count=1` passes today. Record the count; a task that reduces it has broken something.

## Environment

Postgres and Redis are not always running:

```sh
service postgresql start
redis-cli ping || redis-server --daemonize yes
```

A dead Postgres makes unrelated Python tests look broken — the errors are `CalledProcessError` from `sudo -u postgres psql`, not real failures.

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `agent/internal/chunkreply/chunkreply.go` | Correlate an inbound grant to the flush that asked. Nothing else. |
| `agent/internal/chunkreply/chunkreply_test.go` | Its tests. |
| `agent/internal/backupstore/backupstore.go` | Spend a POST grant over HTTPS. Nothing else. |
| `agent/internal/backupstore/backupstore_test.go` | Its tests. |
| `scripts/verify_backup_run.py` | The manual live measurement against real MinIO. |

**Modified:**

| Path | Change |
|---|---|
| `agent/internal/wire/wire.go` | Two kind constants, three types, `Decode` routing, `ChunkQuery.kind()` |
| `agent/internal/wire/wire_test.go` | Framing tests for both kinds |
| `agent/internal/wire/schema_test.go` | Move both titles from `notImplemented` to `implemented`; refresh stale reasons |
| `agent/internal/transport/transport.go` | Add `Options.OnDisconnect`, call it in `Run` |
| `agent/internal/capabilities/backup/dedup.go` | Delete the three local types; use `wire`'s |
| `agent/internal/capabilities/backup/backup.go` | `UploadGrant` → `wire.UploadGrant`; add `Bind` |
| `agent/internal/capabilities/backup/{dedup,budget,helpers,schema}_test.go` | Follow the type move |
| `agent/internal/capabilities/registry.go` | Correct the comment that says backup cannot complete a job |
| `agent/cmd/agent/main.go` | Compose the inbound handler; bind backup |
| `backend/app/modules/backup/dispatch.py` | Derive `manifest_ref` from the reported suffix |
| `backend/tests/test_backup_dispatch.py` | Its tests |
| `docs/modules/backup/spec.md` | Amend §6.1 — items 2 and 3 closed |
| `HANDOFF.md` | Update §2, §5, §6 |

**Task order is a dependency chain.** Task 1 → 2 → {3, 4} → 5 → 6 → 7 → 8. Tasks 3 and 4 are independent of each other and may run in parallel.

---

### Task 1: Teach `wire` the two message kinds

`wire` owns message types; the capability will import them in Task 2. This task adds them and leaves `capabilities/backup` untouched and still compiling against its own copies — the two definitions coexist for exactly one task.

**Files:**
- Modify: `agent/internal/wire/wire.go`
- Modify: `agent/internal/wire/wire_test.go`
- Modify: `agent/internal/wire/schema_test.go:71-133` (the `notImplemented` map)

**Interfaces:**
- Consumes: nothing.
- Produces: `wire.KindBackupChunkQuery`, `wire.KindBackupChunkGrant`, `wire.ChunkQuery`, `wire.ChunkGrant`, `wire.UploadGrant`. `wire.Decode` gains `*wire.ChunkGrant` as a second possible return type.

Exact field/tag requirements, read from the exported schemas (`docs/kernel/wire-protocol/BackupChunkQueryMsg.schema.json`, `BackupChunkGrantMsg.schema.json`):

- `BackupChunkQueryMsg` required: `v`, `kind`, `job_id`, `query_id`. So `check` and `stored` take `omitempty`; `job_id` and `query_id` must **not**.
- `BackupChunkGrantMsg` required: `v`, `kind`, `job_id`, `query_id`, `recorded`, `upload`, `expires_at`. So only `new` takes `omitempty`.
- `BackupUploadGrant` required: all four fields. None takes `omitempty`.

`TestRequiredOutboundPropertiesAreNeverOmitempty` enforces the above, and `TestEveryImplementedSchemaPropertyHasAGoField` enforces both directions of the property set.

- [ ] **Step 1: Write the failing framing tests**

Append to `agent/internal/wire/wire_test.go`:

```go
func TestEncodeFramesAChunkQuery(t *testing.T) {
	got, err := wire.Encode(wire.ChunkQuery{
		JobID: "job-1", QueryID: "q1", Check: []string{"aa"}, Stored: []string{"bb"},
	})
	if err != nil {
		t.Fatalf("Encode: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(got, &m); err != nil {
		t.Fatalf("not JSON: %v", err)
	}
	if m["v"] != float64(1) || m["kind"] != "backup_chunk_query" {
		t.Errorf("framing = v:%v kind:%v, want v:1 kind:backup_chunk_query", m["v"], m["kind"])
	}
	if m["job_id"] != "job-1" || m["query_id"] != "q1" {
		t.Errorf("identity = %v/%v, want job-1/q1", m["job_id"], m["query_id"])
	}
}

// An empty batch must still frame: `check` and `stored` are optional in the
// schema, and a query reporting only `stored` carries neither.
func TestEncodeChunkQueryOmitsEmptyBatches(t *testing.T) {
	got, err := wire.Encode(wire.ChunkQuery{JobID: "job-1", QueryID: "q1"})
	if err != nil {
		t.Fatalf("Encode: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(got, &m); err != nil {
		t.Fatalf("not JSON: %v", err)
	}
	if _, ok := m["check"]; ok {
		t.Error("empty check was emitted; the server is extra=forbid on shape, not on presence")
	}
	if m["job_id"] != "job-1" {
		t.Errorf("job_id = %v, want job-1", m["job_id"])
	}
}

func TestDecodeRoutesAChunkGrant(t *testing.T) {
	raw := []byte(`{"v":1,"kind":"backup_chunk_grant","job_id":"job-1","query_id":"q1",
		"new":["ab"],"recorded":2,"expires_at":"2026-08-09T00:00:00Z",
		"upload":{"url":"https://h/b","fields":{"key":"p/${filename}"},
		"key_prefix":"org/backup/repo/","max_bytes":1024}}`)
	msg, err := wire.Decode(raw)
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}
	g, ok := msg.(*wire.ChunkGrant)
	if !ok {
		t.Fatalf("Decode returned %T, want *wire.ChunkGrant", msg)
	}
	got := [4]any{g.JobID, g.QueryID, len(g.New), g.Upload.MaxBytes}
	want := [4]any{"job-1", "q1", 1, int64(1024)}
	if got != want {
		t.Errorf("grant = %v, want %v", got, want)
	}
}

// Inbound is loose: a field a newer control plane added must not take an old
// fleet offline.
func TestDecodeIgnoresUnknownGrantFields(t *testing.T) {
	raw := []byte(`{"v":1,"kind":"backup_chunk_grant","job_id":"j","query_id":"q",
		"recorded":0,"expires_at":"t","invented_by_a_newer_server":true,
		"upload":{"url":"https://h/b","fields":{},"key_prefix":"p/","max_bytes":1}}`)
	if _, err := wire.Decode(raw); err != nil {
		t.Fatalf("Decode rejected an additive field: %v", err)
	}
}

// The agent must never act on its own outbound kind arriving inbound.
func TestDecodeRefusesAnInboundChunkQuery(t *testing.T) {
	raw := []byte(`{"v":1,"kind":"backup_chunk_query","job_id":"j","query_id":"q"}`)
	if _, err := wire.Decode(raw); !errors.Is(err, wire.ErrKind) {
		t.Errorf("err = %v, want ErrKind", err)
	}
}

// Encode must refuse a grant: it is inbound-only, and framing one would mean
// the agent could mint its own write authorization.
func TestEncodeRefusesAChunkGrant(t *testing.T) {
	if _, err := wire.Encode(wire.ChunkGrant{JobID: "j"}); err == nil {
		t.Error("Encode framed an inbound-only message")
	}
}
```

Ensure `wire_test.go` imports `encoding/json` and `errors`.

- [ ] **Step 2: Run the tests to verify they fail**

```sh
cd agent && go test ./internal/wire/ -run 'ChunkQuery|ChunkGrant' -count=1
```

Expected: compile failure — `undefined: wire.ChunkQuery`. A compile failure is a legitimate red for a type that does not exist yet.

- [ ] **Step 3: Add the kind constants**

In `agent/internal/wire/wire.go`, extend the kind block. Replace the trailing `)` of the existing const block by adding these two lines before it:

```go
	KindHealthReport   = "health_report"
	// The backup dedup exchange (docs/modules/backup/spec.md §5.2). The FIRST
	// server -> agent kind other than KindJob, which is why Decode's contract
	// changes shape here rather than merely gaining a case.
	KindBackupChunkQuery = "backup_chunk_query"
	KindBackupChunkGrant = "backup_chunk_grant"
)
```

Also update the comment above the block — it currently says "Server -> agent has exactly one (KindJob)", which stops being true:

```go
// Message kinds. Server -> agent has exactly two: KindJob, and
// KindBackupChunkGrant (the reply half of the backup dedup exchange). There is
// still no policy channel and no config channel (protocol.py:150-153): a grant
// answers a question this agent asked while running a signed job, and carries
// no instruction.
```

- [ ] **Step 4: Add the three types**

Append to `agent/internal/wire/wire.go`, after the `HealthReport` block and before `Encode`:

```go
// UploadGrant implements BackupChunkGrantMsg $defs BackupUploadGrant: an S3
// POST policy authorising writes under one repository's prefix.
//
// It is a BEARER CREDENTIAL for its lifetime and is treated as one: never
// logged, never written into a job result, and dead with the job. KeyPrefix
// and MaxBytes restate conditions signed INTO the policy — checking them
// locally saves a round trip on a write S3 would reject, and believing them
// changes nothing about what S3 accepts.
type UploadGrant struct {
	URL       string            `json:"url"`
	Fields    map[string]string `json:"fields"`
	KeyPrefix string            `json:"key_prefix"`
	MaxBytes  int64             `json:"max_bytes"`
}

// ChunkQuery implements BackupChunkQueryMsg.
//
// It names ONE thing: the job. No repository id, no storage prefix, no org.
// The server derives all of those from the job it signed, and an agent that
// could name its own repository could name someone else's — in a
// deduplicating store that is a cross-tenant existence oracle.
//
// Check and Stored are omitempty because the schema does not require them: a
// query that only reports uploads carries neither.
type ChunkQuery struct {
	JobID   string   `json:"job_id"`
	QueryID string   `json:"query_id"`
	Check   []string `json:"check,omitempty"`
	Stored  []string `json:"stored,omitempty"`
}

func (ChunkQuery) kind() string { return KindBackupChunkQuery }

// ChunkGrant implements BackupChunkGrantMsg: the dedup answer and the write
// grant for it.
//
// There is deliberately no error variant. Every authorization failure produces
// SILENCE plus a server-side audit record, never a distinguishable reply, so a
// compromised endpoint cannot walk the difference between "no such job" and
// "not your job". The consequence for this agent is that a caller MUST bound
// its wait: see internal/chunkreply.
type ChunkGrant struct {
	JobID     string      `json:"job_id"`
	QueryID   string      `json:"query_id"`
	New       []string    `json:"new,omitempty"`
	Recorded  int64       `json:"recorded"`
	Upload    UploadGrant `json:"upload"`
	ExpiresAt string      `json:"expires_at"`
}
```

`ChunkGrant` deliberately has **no** `kind()` method: that is what makes `Encode` refuse it.

- [ ] **Step 5: Route it in `Decode`**

In `agent/internal/wire/wire.go`, replace the kind check (currently at `:157`):

```go
	if *f.Kind != KindJob {
		return nil, fmt.Errorf("wire: %w: %q", ErrKind, *f.Kind)
	}
```

with:

```go
	if *f.Kind == KindBackupChunkGrant {
		// Unmarshalled from the ORIGINAL bytes, not from `frame`: a grant's
		// fields are top-level, not nested under `job`. Unknown fields are
		// ignored here, which is the inbound-is-loose rule.
		var g ChunkGrant
		if err := json.Unmarshal(b, &g); err != nil {
			return nil, fmt.Errorf("wire: malformed chunk grant: %w", err)
		}
		return &g, nil
	}
	if *f.Kind != KindJob {
		return nil, fmt.Errorf("wire: %w: %q", ErrKind, *f.Kind)
	}
```

Update `Decode`'s doc comment, which currently claims `*Envelope` is the only thing the server sends:

```go
// Decode parses one inbound frame.
//
// It returns *Envelope for a job, or *ChunkGrant for the reply half of the
// backup dedup exchange. Anything else -- an unknown kind, an agent -> server
// kind arriving inbound, a version other than 1 -- is an error: this agent
// never acts on a frame the control plane has no code to have sent.
//
// A caller therefore type-switches. Returning two types is the honest shape:
// making ChunkGrant an Envelope variant would let job-verification code
// receive something that carries no signature.
```

- [ ] **Step 6: Run the tests to verify they pass**

```sh
cd agent && go test ./internal/wire/ -run 'ChunkQuery|ChunkGrant' -v -count=1
```

Expected: all six PASS.

- [ ] **Step 7: Promote both schemas from `notImplemented` to `implemented`**

In `agent/internal/wire/schema_test.go`, **delete** the two `notImplemented` entries `"BackupChunkQueryMsg"` and `"BackupChunkGrantMsg"` together with the comment block above them beginning `// THESE TWO ARE MESSAGE ENVELOPES AND BELONG HERE EVENTUALLY`. Add to the `implemented` map instead:

```go
	"BackupChunkQueryMsg": {
		typ:     reflect.TypeOf(wire.ChunkQuery{}),
		framing: []string{"v", "kind"},
	},
	"BackupChunkGrantMsg": {
		typ:     reflect.TypeOf(wire.ChunkGrant{}),
		framing: []string{"v", "kind"},
		nested:  map[string]reflect.Type{"BackupUploadGrant": reflect.TypeOf(wire.UploadGrant{})},
	},
```

- [ ] **Step 8: Pin the batch bound to the schema rather than to a comment**

Append to `agent/internal/wire/schema_test.go`:

```go
// MAX_CHUNK_BATCH is 1024 on both sides. A server that tightens the bound and
// an agent that does not would send batches the control plane drops at the
// parse layer, and the run would hang on an answer that never comes. Pinned to
// the exported schema so the two cannot drift silently.
func TestChunkBatchBoundMatchesTheSchema(t *testing.T) {
	schemas := loadSchemas(t)
	for _, tc := range []struct{ title, prop string }{
		{"BackupChunkQueryMsg", "check"},
		{"BackupChunkQueryMsg", "stored"},
		{"BackupChunkGrantMsg", "new"},
	} {
		got := schemas[tc.title].Properties[tc.prop].MaxItems
		if got != 1024 {
			t.Errorf("%s.%s maxItems = %d, want 1024", tc.title, tc.prop, got)
		}
	}
}
```

This needs one field added to the `jsonSchema` struct at `schema_test.go:23`:

```go
type jsonSchema struct {
	Title      string                `json:"title"`
	Properties map[string]jsonSchema `json:"properties"`
	Required   []string              `json:"required"`
	Defs       map[string]jsonSchema `json:"$defs"`
	MaxItems   int                   `json:"maxItems"`
}
```

- [ ] **Step 9: Refresh the stale `notImplemented` reasons**

`HANDOFF.md` §6 lists this: several entries still say "no agent capability planned in this plan" for capabilities that are now built. The entries are still *correct* (these are job params and result documents, not message envelopes) but the reason text is false. Replace the reason string on each of these — `AppDeployParams`, `AppDeployResult`, `ScriptRunParams`, `ScriptRunResult`, `BackupManifest`, `BackupRestoreParams`, `BackupRestoreResult`, `BackupRunParams`, `BackupRunResult`, `FiletreeStream`, `ManifestEntry`, `InstallOutcome`, `PatchApplyParams`, `PatchApplyResult`, `PatchScanDocument`, `PatchScanParams`, `ProvisionEnrollParams`, `ProvisionEnrollResult`, `ProvisionPrepareEnvelope` — with the wording the cert/site entries already use, naming the owning package. For example:

```go
	"BackupRunParams":  "job params: unmarshalled by internal/capabilities/backup, not internal/wire",
	"BackupRunResult":  "job result: built by internal/capabilities/backup, not internal/wire",
	"PatchScanParams":  "job params: unmarshalled by internal/capabilities/patch, not internal/wire",
```

Leave `RemoteSessionOpenParams` as-is: `internal/capabilities/` has no `remote` package, so that reason is still true.

- [ ] **Step 10: Run the whole wire suite**

```sh
cd agent && go test ./internal/wire/ -race -count=1 -v 2>&1 | tail -30
```

Expected: PASS, including `TestEveryExportedSchemaIsAccountedFor`, `TestEveryImplementedSchemaPropertyHasAGoField`, `TestRequiredOutboundPropertiesAreNeverOmitempty` and the new `TestChunkBatchBoundMatchesTheSchema`.

- [ ] **Step 11: Falsify the conformance test**

Prove the schema binding is load-bearing rather than decorative. Temporarily change `ChunkQuery`'s `JobID` tag from `json:"job_id"` to `json:"jobid"`. **Confirm the edit applied** (re-read the line), then run:

```sh
cd agent && go test ./internal/wire/ -run TestEveryImplementedSchemaPropertyHasAGoField -count=1
```

Expected: FAIL naming `job_id`. Revert the tag and re-run to confirm PASS.

- [ ] **Step 12: Commit**

```sh
cd /home/user/Toeshee-RMS && git reset
git add agent/internal/wire/wire.go agent/internal/wire/wire_test.go agent/internal/wire/schema_test.go
git commit -m "feat(agent/wire): frame the backup dedup exchange

Adds backup_chunk_query (7th outbound kind) and backup_chunk_grant (the
second server -> agent kind ever). Decode now returns *Envelope or
*ChunkGrant; Encode refuses a grant, so an agent cannot mint its own write
authorization. Both schemas move from notImplemented into implemented, and
MAX_CHUNK_BATCH is pinned to the exported maxItems rather than to a comment.

Also refreshes notImplemented reasons that still claimed no agent capability
was planned for capabilities that are now built (HANDOFF.md section 6)."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 2: Point `capabilities/backup` at the wire types

Delete the capability's local copies and use `wire`'s. Behaviour must not change: this task is green when the backup suite passes exactly as before.

**Files:**
- Modify: `agent/internal/capabilities/backup/dedup.go:66-114` (the three type declarations)
- Modify: `agent/internal/capabilities/backup/backup.go` (the `Put` signature and `Options`)
- Modify: `agent/internal/capabilities/backup/{dedup,budget,helpers,schema}_test.go`

**Interfaces:**
- Consumes: `wire.ChunkQuery`, `wire.ChunkGrant`, `wire.UploadGrant` (Task 1).
- Produces: `backup.QueryChunks` is now `func(context.Context, wire.ChunkQuery) (wire.ChunkGrant, error)`. `backup.ObjectStore.Put` is now `Put(ctx context.Context, grant wire.UploadGrant, suffix string, data []byte) (string, error)`.

- [ ] **Step 1: Delete the local types**

In `agent/internal/capabilities/backup/dedup.go`, delete the `UploadGrant`, `ChunkQuery` and `ChunkGrant` struct declarations (their doc comments move to `wire` in Task 1 — do not duplicate them). Keep `maxChunkBatch`, keep the package doc comment, and keep `pendingChunk`.

Change the `QueryChunks` type:

```go
// QueryChunks is the transport seam for one exchange: send a wire.ChunkQuery
// over the agent's authenticated connection and return the wire.ChunkGrant
// that comes back.
//
// It is an INJECTED SEAM because the transport lives outside this package, and
// a capability that dialled anything itself would be a second path out of the
// endpoint. internal/chunkreply implements it.
type QueryChunks func(ctx context.Context, q wire.ChunkQuery) (wire.ChunkGrant, error)
```

Add the import `"github.com/toeshee/it-suite/agent/internal/wire"`.

- [ ] **Step 2: Update every reference**

Replace `UploadGrant` → `wire.UploadGrant`, `ChunkQuery` → `wire.ChunkQuery`, `ChunkGrant` → `wire.ChunkGrant` throughout `dedup.go`, `backup.go` and the four test files. There are ~55 references across 7 files.

In `backup.go`, `ObjectStore.Put`'s signature becomes:

```go
	Put(ctx context.Context, grant wire.UploadGrant, suffix string, data []byte) (ref string, err error)
```

- [ ] **Step 3: Build and run the backup suite**

```sh
cd agent && go build ./... && go test ./internal/capabilities/backup/ -race -count=1
```

Expected: PASS, with the same test count as the Task-1 baseline. A behaviour change here is a bug — this task only moves types.

- [ ] **Step 4: Confirm no duplicate definitions survive**

```sh
cd agent && grep -rn "^type \(UploadGrant\|ChunkQuery\|ChunkGrant\) struct" internal/
```

Expected: three hits, all in `internal/wire/wire.go`. Zero in `internal/capabilities/backup/`.

- [ ] **Step 5: Commit**

```sh
cd /home/user/Toeshee-RMS && git reset
git add agent/internal/capabilities/backup/
git commit -m "refactor(agent/backup): use wire's message types, not local copies

The capability declared its own ChunkQuery/ChunkGrant/UploadGrant because
wire had no framing for them. It does now, so these become one definition
pinned to the exported schema instead of two that can drift. Follows the
convention capabilities/logs and capabilities/health already set: import
wire, build wire types, take the transport as an injected seam.

Pure type move; no behaviour change."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 3: The `chunkreply` correlation registry

**Files:**
- Create: `agent/internal/chunkreply/chunkreply.go`
- Create: `agent/internal/chunkreply/chunkreply_test.go`
- Modify: `agent/internal/transport/transport.go` (add `Options.OnDisconnect`; call it in `Run`)

**Interfaces:**
- Consumes: `wire.ChunkQuery`, `wire.ChunkGrant` (Task 1).
- Produces:
  - `chunkreply.New(log *slog.Logger) *Registry`
  - `(*Registry).Attach(s Sender)` — install the live connection
  - `(*Registry).Detach(err error)` — fail every pending wait
  - `(*Registry).Deliver(g *wire.ChunkGrant) bool` — route an inbound grant; false if unmatched
  - `(*Registry).Query(ctx context.Context, q wire.ChunkQuery) (wire.ChunkGrant, error)` — satisfies `backup.QueryChunks`
  - `(*Registry).Deadline time.Duration` — settable before use; zero means `DefaultDeadline`
  - `chunkreply.DefaultDeadline = 60 * time.Second`, `chunkreply.ErrNoConnection`, `chunkreply.ErrDisconnected`, `chunkreply.ErrTimeout`, `chunkreply.ErrTooManyPending`

The registry keys on the **pair** `(job_id, query_id)`. `dedup.go:196` mints `query_id` as a per-session counter that restarts at `q1` for every run, so keying on `query_id` alone could hand a late grant from a dead run to a new run's identically-numbered flush.

- [ ] **Step 1: Write the failing tests**

Create `agent/internal/chunkreply/chunkreply_test.go`:

```go
package chunkreply_test

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"runtime"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/chunkreply"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

func quiet() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// fakeSender records what was sent and optionally answers.
type fakeSender struct {
	mu   sync.Mutex
	sent []wire.ChunkQuery
	err  error
}

func (f *fakeSender) Send(_ context.Context, msg any) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return f.err
	}
	f.sent = append(f.sent, msg.(wire.ChunkQuery))
	return nil
}

func (f *fakeSender) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.sent)
}

func TestQueryReturnsTheMatchingGrant(t *testing.T) {
	r := chunkreply.New(quiet())
	s := &fakeSender{}
	r.Attach(s)

	go func() {
		for r.Pending() == 0 {
			runtime.Gosched()
		}
		r.Deliver(&wire.ChunkGrant{JobID: "job-1", QueryID: "q1", New: []string{"ab"}, Recorded: 3})
	}()

	got, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "job-1", QueryID: "q1"})
	if err != nil {
		t.Fatalf("Query: %v", err)
	}
	state := [3]any{got.QueryID, len(got.New), got.Recorded}
	want := [3]any{"q1", 1, int64(3)}
	if state != want {
		t.Errorf("grant = %v, want %v", state, want)
	}
	if s.count() != 1 {
		t.Errorf("sent %d queries, want 1", s.count())
	}
}

// Silence IS the refusal (protocol.py:259). Without a deadline an
// unauthorized backup.run hangs forever instead of failing.
func TestQueryTimesOutOnSilence(t *testing.T) {
	r := chunkreply.New(quiet())
	r.Deadline = 20 * time.Millisecond
	r.Attach(&fakeSender{})

	start := time.Now()
	_, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "j", QueryID: "q1"})
	if !errors.Is(err, chunkreply.ErrTimeout) {
		t.Fatalf("err = %v, want ErrTimeout", err)
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Errorf("waited %v; the deadline did not bound the wait", elapsed)
	}
	if r.Pending() != 0 {
		t.Errorf("pending = %d after timeout, want 0 (the entry leaked)", r.Pending())
	}
}

// A reply cannot arrive on a new connection, so the wait must not burn the
// full deadline after a drop.
func TestDetachFailsPendingWaitsImmediately(t *testing.T) {
	r := chunkreply.New(quiet())
	r.Deadline = time.Hour
	r.Attach(&fakeSender{})

	done := make(chan error, 1)
	go func() {
		_, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "j", QueryID: "q1"})
		done <- err
	}()
	for r.Pending() == 0 {
		runtime.Gosched()
	}
	r.Detach(errors.New("socket closed"))

	select {
	case err := <-done:
		if !errors.Is(err, chunkreply.ErrDisconnected) {
			t.Errorf("err = %v, want ErrDisconnected", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Detach did not wake the pending wait")
	}
}

// The pair is the key: a counter restarts at q1 every run.
func TestDeliverMatchesOnJobAndQueryTogether(t *testing.T) {
	r := chunkreply.New(quiet())
	r.Deadline = 50 * time.Millisecond
	r.Attach(&fakeSender{})

	done := make(chan error, 1)
	go func() {
		_, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "job-A", QueryID: "q1"})
		done <- err
	}()
	for r.Pending() == 0 {
		runtime.Gosched()
	}
	// Same query id, different job: a late grant from a previous run.
	if r.Deliver(&wire.ChunkGrant{JobID: "job-B", QueryID: "q1"}) {
		t.Error("a grant for another job was routed to this waiter")
	}
	if err := <-done; !errors.Is(err, chunkreply.ErrTimeout) {
		t.Errorf("err = %v, want ErrTimeout", err)
	}
}

func TestDeliverDropsUnmatchedAndDuplicateGrants(t *testing.T) {
	r := chunkreply.New(quiet())
	r.Attach(&fakeSender{})

	if r.Deliver(&wire.ChunkGrant{JobID: "j", QueryID: "nobody-waiting"}) {
		t.Error("an unmatched grant reported as delivered")
	}

	go func() {
		for r.Pending() == 0 {
			runtime.Gosched()
		}
		r.Deliver(&wire.ChunkGrant{JobID: "j", QueryID: "q1"})
		if r.Deliver(&wire.ChunkGrant{JobID: "j", QueryID: "q1"}) {
			t.Error("a duplicate grant reported as delivered")
		}
	}()
	if _, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "j", QueryID: "q1"}); err != nil {
		t.Fatalf("Query: %v", err)
	}
}

func TestQueryWithoutAConnectionFailsFast(t *testing.T) {
	r := chunkreply.New(quiet())
	_, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "j", QueryID: "q1"})
	if !errors.Is(err, chunkreply.ErrNoConnection) {
		t.Errorf("err = %v, want ErrNoConnection", err)
	}
}

// The idle budget: a registry that has never been queried holds nothing and
// runs nothing.
func TestIdleRegistryHoldsNothing(t *testing.T) {
	before := runtime.NumGoroutine()
	r := chunkreply.New(quiet())
	r.Attach(&fakeSender{})
	runtime.Gosched()
	if got := runtime.NumGoroutine(); got > before {
		t.Errorf("goroutines %d -> %d; the registry started one at rest", before, got)
	}
	if r.Pending() != 0 {
		t.Errorf("pending = %d at rest, want 0", r.Pending())
	}
}
```

- [ ] **Step 2: Run the tests to verify they fail**

```sh
cd agent && go test ./internal/chunkreply/ -count=1
```

Expected: build failure — the package does not exist.

- [ ] **Step 3: Implement the registry**

Create `agent/internal/chunkreply/chunkreply.go`:

```go
// Package chunkreply correlates one inbound backup_chunk_grant to the flush
// that asked for it.
//
// # Why this exists at all
//
// The agent's transport is one-way: transport.Run reads frames and hands each
// to a handler. The backup dedup exchange (docs/modules/backup/spec.md §5.2)
// is request/response — a flush sends a query and must block until the grant
// bearing ITS ids comes back — and nothing else in this agent correlates a
// reply to a request.
//
// # Why silence is the hard part
//
// The control plane answers an unauthorized query with NOTHING (protocol.py:259):
// unknown job, another device's job, a job that is not a backup.run, an expired
// job, a finished run. A distinguishable error would be an oracle a compromised
// endpoint could walk. So a wait here MUST be bounded, or an endpoint whose job
// the server refuses hangs forever instead of failing a run an operator can see.
//
// # The idle budget
//
// A registry at rest is an empty map and a mutex. No goroutine, no timer, no
// socket, no allocation until a query is in flight.
package chunkreply

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// DefaultDeadline bounds one exchange. Generous for a server-side index lookup
// over at most 1024 ids, short enough that a refused job fails visibly rather
// than looking hung.
const DefaultDeadline = 60 * time.Second

// maxPending caps concurrent in-flight queries. One run flushes serially, so
// the realistic count is 1; the cap turns a bug that spawned unbounded flushes
// into an immediate, named failure rather than quiet memory growth on someone's
// desktop.
const maxPending = 16

var (
	ErrNoConnection   = errors.New("chunkreply: no gateway connection")
	ErrDisconnected   = errors.New("chunkreply: gateway connection lost while awaiting a grant")
	ErrTimeout        = errors.New("chunkreply: no grant returned before the deadline")
	ErrTooManyPending = errors.New("chunkreply: too many queries in flight")
)

// Sender is the write half of the agent's authenticated connection.
// *transport.Conn satisfies it. Deliberately narrow: this package can send a
// message and can do nothing else with the socket.
type Sender interface {
	Send(ctx context.Context, msg any) error
}

// key is (job_id, query_id). query_id alone is not unique: dedup.go mints it as
// a per-session counter that restarts at "q1" for every run, so a late grant
// from a dead run would otherwise match a new run's first flush.
type key struct{ job, query string }

type Registry struct {
	// Deadline bounds one exchange. Zero means DefaultDeadline. Set before use;
	// it is read under the lock but not intended to change while queries are in
	// flight.
	Deadline time.Duration

	log *slog.Logger

	mu      sync.Mutex
	conn    Sender
	pending map[key]chan *wire.ChunkGrant
}

func New(log *slog.Logger) *Registry {
	if log == nil {
		log = slog.Default()
	}
	return &Registry{log: log, pending: map[key]chan *wire.ChunkGrant{}}
}

// Attach installs the live connection. Called from transport's OnConnected.
func (r *Registry) Attach(s Sender) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.conn = s
}

// Detach drops the connection and fails every pending wait.
//
// Immediately, rather than letting each burn its deadline: a grant is answered
// on the connection the query went out on, so a reply cannot arrive after this.
func (r *Registry) Detach(cause error) {
	r.mu.Lock()
	waiters := make([]chan *wire.ChunkGrant, 0, len(r.pending))
	for k, ch := range r.pending {
		waiters = append(waiters, ch)
		delete(r.pending, k)
	}
	r.conn = nil
	r.mu.Unlock()

	for _, ch := range waiters {
		close(ch)
	}
	if len(waiters) > 0 {
		r.log.Warn("gateway connection lost with backup dedup queries in flight",
			"pending", len(waiters), "cause", cause)
	}
}

// Deliver routes an inbound grant. It reports whether anything was waiting.
//
// An unmatched grant is dropped rather than treated as an error: it is a late
// reply to a query that already timed out, which is expected rather than
// exceptional.
func (r *Registry) Deliver(g *wire.ChunkGrant) bool {
	if g == nil {
		return false
	}
	k := key{job: g.JobID, query: g.QueryID}
	r.mu.Lock()
	ch, ok := r.pending[k]
	if ok {
		delete(r.pending, k)
	}
	r.mu.Unlock()
	if !ok {
		// Neither id is logged: query_id is agent-minted and job_id is already
		// in the run's own logging. Logging a whole grant would log a bearer
		// credential.
		r.log.Debug("dropped a backup chunk grant that nothing was waiting for")
		return false
	}
	ch <- g
	return true
}

// Pending reports how many queries are in flight. For tests and diagnostics.
func (r *Registry) Pending() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.pending)
}

// Query runs one exchange. It satisfies backup.QueryChunks.
func (r *Registry) Query(ctx context.Context, q wire.ChunkQuery) (wire.ChunkGrant, error) {
	k := key{job: q.JobID, query: q.QueryID}

	r.mu.Lock()
	conn := r.conn
	if conn == nil {
		r.mu.Unlock()
		return wire.ChunkGrant{}, ErrNoConnection
	}
	if len(r.pending) >= maxPending {
		r.mu.Unlock()
		return wire.ChunkGrant{}, ErrTooManyPending
	}
	if _, dup := r.pending[k]; dup {
		r.mu.Unlock()
		return wire.ChunkGrant{}, fmt.Errorf("chunkreply: %q is already in flight", q.QueryID)
	}
	// Buffered: Deliver must never block on a waiter that has given up.
	ch := make(chan *wire.ChunkGrant, 1)
	r.pending[k] = ch
	r.mu.Unlock()

	if err := conn.Send(ctx, q); err != nil {
		r.forget(k)
		return wire.ChunkGrant{}, fmt.Errorf("chunkreply: sending the query: %w", err)
	}

	deadline := r.Deadline
	if deadline <= 0 {
		deadline = DefaultDeadline
	}
	timer := time.NewTimer(deadline)
	defer timer.Stop()

	select {
	case g, ok := <-ch:
		if !ok {
			return wire.ChunkGrant{}, ErrDisconnected
		}
		return *g, nil
	case <-ctx.Done():
		r.forget(k)
		return wire.ChunkGrant{}, ctx.Err()
	case <-timer.C:
		r.forget(k)
		// Named explicitly, because the most likely cause is not a slow server:
		// it is the control plane refusing this query in silence.
		return wire.ChunkGrant{}, fmt.Errorf(
			"%w after %s. The control plane answers an unauthorized dedup query with "+
				"silence rather than an error, so this is what a refused job looks like",
			ErrTimeout, deadline)
	}
}

func (r *Registry) forget(k key) {
	r.mu.Lock()
	delete(r.pending, k)
	r.mu.Unlock()
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```sh
cd agent && go test ./internal/chunkreply/ -race -count=1 -v 2>&1 | tail -20
```

Expected: all eight PASS.

- [ ] **Step 5: Add the transport disconnect hook**

In `agent/internal/transport/transport.go`, add to `Options` immediately after `OnConnected`:

```go
	// OnDisconnect runs once per connection loss, after the connection is
	// closed and before the reconnect backoff.
	//
	// It exists for callers holding state that is only meaningful on ONE
	// connection -- the backup dedup exchange is the first: a query's answer
	// comes back on the connection it went out on, so a pending wait must fail
	// when that connection dies rather than burning its full deadline waiting
	// for a reply that cannot arrive.
	//
	// It receives the cause and can send nothing: like OnConnect, it is not a
	// message hook and cannot become a second inbound path.
	//
	// COMPATIBILITY: additive Go field, not a wire change. A nil hook is the
	// previous behaviour exactly.
	OnDisconnect func(context.Context, error)
```

In `Run`, replace:

```go
			if err == nil {
				err = conn.serve(ctx, opts, handler)
			}
			_ = conn.Close()
		}
```

with:

```go
			if err == nil {
				err = conn.serve(ctx, opts, handler)
			}
			_ = conn.Close()
			if opts.OnDisconnect != nil {
				opts.OnDisconnect(ctx, err)
			}
		}
```

- [ ] **Step 6: Test the hook**

Append to `agent/internal/transport/transport_test.go`:

```go
// A caller holding per-connection state must learn when that connection dies.
func TestOnDisconnectFiresWhenTheConnectionDrops(t *testing.T) {
	var mu sync.Mutex
	calls := 0
	srv := newTestGateway(t)
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		_ = transport.Run(ctx, testOptions(t, srv, transport.Options{
			OnConnected: func(context.Context, *transport.Conn) error {
				srv.CloseClientConnections()
				return nil
			},
			OnDisconnect: func(context.Context, error) {
				mu.Lock()
				calls++
				mu.Unlock()
			},
		}), func(context.Context, []byte) {})
	}()

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		n := calls
		mu.Unlock()
		if n > 0 {
			cancel()
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	t.Fatal("OnDisconnect never fired")
}
```

**Adapt this to the file's existing helpers.** `transport_test.go` already stands up a gateway and builds `Options`; reuse whatever it calls them rather than introducing `newTestGateway`/`testOptions` if they do not exist. Read the file first.

- [ ] **Step 7: Run the transport suite**

```sh
cd agent && go test ./internal/transport/ -race -count=1 2>&1 | tail -10
```

Expected: PASS, including every pre-existing test — a nil `OnDisconnect` must be exactly the previous behaviour.

- [ ] **Step 8: Falsify the deadline**

Temporarily change `Query`'s `case <-timer.C:` branch to `case <-make(chan time.Time):` (a channel that never fires). **Confirm the edit applied**, then:

```sh
cd agent && go test ./internal/chunkreply/ -run TestQueryTimesOutOnSilence -timeout 30s -count=1
```

Expected: FAIL (panic: test timed out) — proving the deadline is what bounds the wait. Revert and re-run to confirm PASS.

- [ ] **Step 9: Commit**

```sh
cd /home/user/Toeshee-RMS && git reset
git add agent/internal/chunkreply/ agent/internal/transport/transport.go agent/internal/transport/transport_test.go
git commit -m "feat(agent): correlate a backup chunk grant to the flush that asked

The transport is one-way and the dedup exchange is request/response, so
nothing correlated a reply to a request. chunkreply keys pending waits on
(job_id, query_id) -- not query_id alone, which dedup.go mints as a counter
that restarts at q1 every run, so a late grant from a dead run would match a
new run's first flush.

Every wait is bounded, because the control plane answers an unauthorized
query with SILENCE rather than an error (protocol.py:259). Without a deadline
a refused backup.run hangs forever instead of failing where an operator can
see it. A dropped connection fails pending waits immediately, via a new
transport OnDisconnect hook following the three hooks already there: additive
Go field, nil is the previous behaviour exactly.

At rest the registry is an empty map and a mutex -- no goroutine, no timer."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 4: The POST-grant object store

**Files:**
- Create: `agent/internal/backupstore/backupstore.go`
- Create: `agent/internal/backupstore/backupstore_test.go`

**Interfaces:**
- Consumes: `wire.UploadGrant` (Task 1), `backup.ObjectStore` and `backup.Repository` (Task 2).
- Produces: `backupstore.Open(ctx context.Context, repo backup.Repository) (backup.ObjectStore, error)` — assignable to `backup.Options.OpenStore`.

The S3 POST form: `grant.Fields` verbatim, with `key` **overridden** to the literal full key. boto3 returns `key` as `{prefix}/${filename}`, a placeholder; the signed condition is `["starts-with", "$key", "{prefix}/"]`, so any conforming literal key is valid. The full key is `grant.KeyPrefix + suffix` — `key_prefix` already carries its trailing slash (`dedup.py:360`). The file part must be **last**; S3 ignores anything after it.

- [ ] **Step 1: Write the failing tests**

Create `agent/internal/backupstore/backupstore_test.go`:

```go
package backupstore_test

import (
	"bytes"
	"context"
	"mime"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/backupstore"
	"github.com/toeshee/it-suite/agent/internal/capabilities/backup"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

func grantFor(url string) wire.UploadGrant {
	return wire.UploadGrant{
		URL:       url,
		Fields:    map[string]string{"key": "org/backup/repo/${filename}", "policy": "signed", "x-amz-signature": "sig"},
		KeyPrefix: "org/backup/repo/",
		MaxBytes:  1 << 20,
	}
}

// captured records what the fake object store received.
type captured struct {
	fields map[string]string
	body   string
	order  []string
}

func capturingServer(t *testing.T, status int) (*httptest.Server, *captured) {
	t.Helper()
	got := &captured{fields: map[string]string{}}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, params, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if err != nil {
			t.Errorf("Content-Type: %v", err)
			w.WriteHeader(400)
			return
		}
		mr := multipart.NewReader(r.Body, params["boundary"])
		for {
			p, err := mr.NextPart()
			if err != nil {
				break
			}
			// bytes.Buffer, not strings.Builder: only the former has ReadFrom.
			buf := new(bytes.Buffer)
			_, _ = buf.ReadFrom(p)
			got.order = append(got.order, p.FormName())
			if p.FormName() == "file" {
				got.body = buf.String()
			} else {
				got.fields[p.FormName()] = buf.String()
			}
		}
		w.WriteHeader(status)
	}))
	return srv, got
}

func TestPutSendsTheSignedFieldsWithALiteralKey(t *testing.T) {
	srv, got := capturingServer(t, 204)
	defer srv.Close()

	st, err := backupstore.Open(context.Background(), backup.Repository{ID: "repo", StorageRef: "org/backup/repo"})
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer st.Close()

	ref, err := st.Put(context.Background(), grantFor(srv.URL), "chunks/ab/abcd", []byte("sealed"))
	if err != nil {
		t.Fatalf("Put: %v", err)
	}

	state := [4]any{got.fields["key"], got.fields["policy"], got.body, ref}
	want := [4]any{"org/backup/repo/chunks/ab/abcd", "signed", "sealed", "chunks/ab/abcd"}
	if state != want {
		t.Errorf("upload = %v, want %v", state, want)
	}
	if len(got.order) == 0 || got.order[len(got.order)-1] != "file" {
		t.Errorf("part order = %v; `file` must be last or S3 ignores what follows", got.order)
	}
}

// A grant is scoped to a prefix. A suffix that escapes it must never reach the
// network -- S3 would reject it, but only after the bytes were sent.
func TestPutRefusesASuffixThatEscapesThePrefix(t *testing.T) {
	srv, got := capturingServer(t, 204)
	defer srv.Close()
	st, _ := backupstore.Open(context.Background(), backup.Repository{ID: "repo"})
	defer st.Close()

	for _, bad := range []string{"../elsewhere/x", "/absolute", "a/../../b", ""} {
		if _, err := st.Put(context.Background(), grantFor(srv.URL), bad, []byte("x")); err == nil {
			t.Errorf("Put(%q) was accepted", bad)
		}
	}
	if len(got.order) != 0 {
		t.Errorf("a refused suffix still reached the network: %v", got.order)
	}
}

func TestPutRefusesABodyOverTheGrantCap(t *testing.T) {
	srv, got := capturingServer(t, 204)
	defer srv.Close()
	st, _ := backupstore.Open(context.Background(), backup.Repository{ID: "repo"})
	defer st.Close()

	g := grantFor(srv.URL)
	g.MaxBytes = 4
	if _, err := st.Put(context.Background(), g, "chunks/ab/x", []byte("more than four")); err == nil {
		t.Error("a body over max_bytes was accepted")
	}
	if len(got.order) != 0 {
		t.Errorf("an oversized body still reached the network: %v", got.order)
	}
}

func TestPutRefusesAPlaintextGrantURL(t *testing.T) {
	st, _ := backupstore.Open(context.Background(), backup.Repository{ID: "repo"})
	defer st.Close()
	g := grantFor("http://insecure.example/bucket")
	if _, err := st.Put(context.Background(), g, "chunks/ab/x", []byte("x")); err == nil {
		t.Error("a plaintext grant URL was accepted")
	}
}

// A 302 turns a fixed-host client into an arbitrary-host one.
func TestPutNeverFollowsARedirect(t *testing.T) {
	var reached bool
	elsewhere := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached = true
		w.WriteHeader(204)
	}))
	defer elsewhere.Close()
	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, elsewhere.URL, http.StatusFound)
	}))
	defer redirector.Close()

	st, _ := backupstore.Open(context.Background(), backup.Repository{ID: "repo"})
	defer st.Close()
	if _, err := st.Put(context.Background(), grantFor(redirector.URL), "chunks/ab/x", []byte("x")); err == nil {
		t.Error("a redirected upload was accepted")
	}
	if reached {
		t.Error("the upload followed a redirect to another host")
	}
}

// A 403 is the signed policy's verdict, not a transient. Retrying it wastes an
// endpoint's bandwidth on an answer that will not change.
func TestPutDoesNotRetryAPolicyRejection(t *testing.T) {
	var attempts int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		attempts++
		w.WriteHeader(403)
	}))
	defer srv.Close()

	st, _ := backupstore.Open(context.Background(), backup.Repository{ID: "repo"})
	defer st.Close()
	if _, err := st.Put(context.Background(), grantFor(srv.URL), "chunks/ab/x", []byte("x")); err == nil {
		t.Fatal("a 403 was treated as success")
	}
	if attempts != 1 {
		t.Errorf("attempts = %d, want 1", attempts)
	}
}

// Restore is not built. The refusal must survive OpenStore becoming non-nil,
// or backup.restore stops refusing and starts failing somewhere less legible.
func TestGetAndGetRefStillRefuse(t *testing.T) {
	st, _ := backupstore.Open(context.Background(), backup.Repository{ID: "repo"})
	defer st.Close()

	if _, err := st.Get(context.Background(), "chunks/ab/x"); err == nil {
		t.Error("Get did not refuse")
	}
	if _, err := st.GetRef(context.Background(), "s3://b/k"); err == nil {
		t.Error("GetRef did not refuse")
	}
}

// The idle budget: Open must not dial. It is called at job time, but the store
// it returns must not touch the network until Put does.
func TestOpenDialsNothing(t *testing.T) {
	var reached bool
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		reached = true
	}))
	defer srv.Close()
	st, err := backupstore.Open(context.Background(), backup.Repository{ID: "repo", StorageRef: srv.URL})
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer st.Close()
	if reached {
		t.Error("Open made a request")
	}
}
```

- [ ] **Step 2: Run the tests to verify they fail**

```sh
cd agent && go test ./internal/backupstore/ -count=1
```

Expected: build failure — package does not exist.

- [ ] **Step 3: Implement the store**

Create `agent/internal/backupstore/backupstore.go`:

```go
// Package backupstore spends the control plane's prefix-scoped S3 POST grant.
//
// # What this package is allowed to be
//
// This agent holds NO object-store credential and must never be given one: a
// long-lived key with write access to every tenant's objects, on a machine
// whose threat model is "may be compromised", is invariant #3 inverted. Every
// write here is authorized by a short-lived POST policy the control plane
// minted for one job, one repository and one prefix, and handed back on the
// dedup exchange. Nothing is held between jobs.
//
// # A second trust domain, and why it is acceptable
//
// The gateway is reached on :8443 with a certificate pinned to the internal CA
// at enrollment. Object storage is reached on :443 with the operator's
// publicly-trusted certificate, which the internal CA cannot validate, so this
// client uses the SYSTEM trust store. That is a real widening: a machine with a
// hostile root CA installed could MITM an upload.
//
// It is bounded because the payload is ALREADY SEALED with the repository key
// before it reaches this package. A MITM sees ciphertext; it can withhold or
// corrupt an upload, not read one. That property is what makes system roots
// acceptable here, so it must not be "simplified" later by moving encryption
// server-side.
//
// READS ARE NOT IMPLEMENTED. SigV4 query signing cannot express a
// prefix-scoped GET and a restore's selection can run to six figures of chunks,
// so Get/GetRef refuse (docs/modules/backup/spec.md §6.1, still-open item 1).
package backupstore

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/toeshee/it-suite/agent/internal/capabilities/backup"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

const (
	// uploadTimeout bounds one object upload, end to end.
	uploadTimeout = 5 * time.Minute
	// maxAttempts is 1 try plus 2 retries, for TRANSPORT errors only.
	maxAttempts = 3
)

var retryBackoff = []time.Duration{time.Second, 2 * time.Second}

// Open returns a job-scoped store. It dials nothing: the first byte leaves in
// Put. Assignable to backup.Options.OpenStore.
func Open(_ context.Context, _ backup.Repository) (backup.ObjectStore, error) {
	return &store{
		client: &http.Client{
			Timeout: uploadTimeout,
			// A 302 would turn a client aimed at one signed endpoint into an
			// arbitrary-host client. The grant's URL is the only address this
			// package may talk to.
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return errors.New("backupstore: refused to follow a redirect away from the granted endpoint")
			},
		},
	}, nil
}

type store struct{ client *http.Client }

func (s *store) Close() error {
	s.client.CloseIdleConnections()
	return nil
}

// safeSuffix rejects anything that would escape the granted prefix.
//
// S3 would reject it too -- the policy's `starts-with $key` condition is
// enforced by the object store, not here -- but only after the body crossed the
// wire. This is the cheap half of the check, not the authoritative one.
func safeSuffix(suffix string) error {
	switch {
	case suffix == "":
		return errors.New("backupstore: empty object suffix")
	case strings.HasPrefix(suffix, "/"), strings.HasPrefix(suffix, `\`):
		return fmt.Errorf("backupstore: absolute object suffix %q", suffix)
	case strings.Contains(suffix, `\`):
		return fmt.Errorf("backupstore: backslash in object suffix %q", suffix)
	}
	for _, seg := range strings.Split(suffix, "/") {
		if seg == "" || seg == "." || seg == ".." {
			return fmt.Errorf("backupstore: unsafe segment in object suffix %q", suffix)
		}
	}
	return nil
}

func (s *store) Put(ctx context.Context, grant wire.UploadGrant, suffix string, data []byte) (string, error) {
	if err := safeSuffix(suffix); err != nil {
		return "", err
	}
	if grant.MaxBytes > 0 && int64(len(data)) > grant.MaxBytes {
		return "", fmt.Errorf("backupstore: object is %d bytes, over the grant's %d-byte cap",
			len(data), grant.MaxBytes)
	}
	u, err := url.Parse(grant.URL)
	if err != nil {
		return "", fmt.Errorf("backupstore: unparseable grant endpoint: %w", err)
	}
	if u.Scheme != "https" {
		// Never downgraded, not even for a lab: a grant is a bearer credential
		// and the form carries it in the clear.
		return "", fmt.Errorf("backupstore: grant endpoint is %q, and only https is allowed", u.Scheme)
	}

	key := grant.KeyPrefix + suffix
	for attempt := 0; ; attempt++ {
		body, contentType, err := buildForm(grant, key, data)
		if err != nil {
			return "", err
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, grant.URL, bytes.NewReader(body))
		if err != nil {
			return "", err
		}
		req.Header.Set("Content-Type", contentType)

		resp, err := s.client.Do(req)
		if err == nil {
			_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4<<10))
			_ = resp.Body.Close()
			if resp.StatusCode >= 200 && resp.StatusCode < 300 {
				return suffix, nil
			}
			// 4xx is the signed policy's verdict -- wrong prefix, over the size
			// cap, expired. It will not change on a retry, and the response body
			// is deliberately not included: it can echo request fields.
			if resp.StatusCode < 500 {
				return "", fmt.Errorf("backupstore: the object store refused the upload with %d "+
					"(the signed policy's verdict, not a transient)", resp.StatusCode)
			}
			err = fmt.Errorf("backupstore: object store returned %d", resp.StatusCode)
		}
		if attempt >= maxAttempts-1 {
			return "", fmt.Errorf("backupstore: upload failed after %d attempts: %w", maxAttempts, err)
		}
		// Safe to retry: the store is content-addressed, so this rewrites
		// identical bytes to the same key.
		select {
		case <-time.After(retryBackoff[attempt]):
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
}

// buildForm assembles the S3 POST body. `file` MUST be the last part: S3
// ignores every field after it.
func buildForm(grant wire.UploadGrant, key string, data []byte) ([]byte, string, error) {
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	// `key` is overridden rather than copied: boto3 returns it as the
	// placeholder "{prefix}/${filename}", and the signed condition is
	// `starts-with $key "{prefix}/"`, which any conforming literal satisfies.
	if err := w.WriteField("key", key); err != nil {
		return nil, "", err
	}
	for k, v := range grant.Fields {
		if k == "key" {
			continue
		}
		if err := w.WriteField(k, v); err != nil {
			return nil, "", err
		}
	}
	part, err := w.CreateFormFile("file", "object")
	if err != nil {
		return nil, "", err
	}
	if _, err := part.Write(data); err != nil {
		return nil, "", err
	}
	if err := w.Close(); err != nil {
		return nil, "", err
	}
	return buf.Bytes(), w.FormDataContentType(), nil
}

const readRefusal = "backup: this agent cannot read from the repository. Writes travel " +
	"under the prefix-scoped POST grant the control plane returns on the dedup exchange, " +
	"but SigV4 query signing cannot express a prefix-scoped READ, and a restore's chunk " +
	"selection can run to six figures -- one presigned URL each is tens of megabytes of " +
	"job params. See docs/modules/backup/spec.md §6.1, still-open item 1"

func (s *store) Get(context.Context, string) ([]byte, error) {
	return nil, jobs.Refuse(readRefusal)
}

func (s *store) GetRef(context.Context, string) ([]byte, error) {
	return nil, jobs.Refuse(readRefusal)
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```sh
cd agent && go test ./internal/backupstore/ -race -count=1 -v 2>&1 | tail -20
```

Expected: all eight PASS.

- [ ] **Step 5: Falsify the prefix guard**

Temporarily change `safeSuffix` to `return nil` as its first statement. **Confirm the edit applied**, then:

```sh
cd agent && go test ./internal/backupstore/ -run TestPutRefusesASuffixThatEscapesThePrefix -count=1
```

Expected: FAIL, and the failure must name the network assertion (`a refused suffix still reached the network`) — proving the test observes the *world*, not merely a returned error. Revert and re-run to confirm PASS.

- [ ] **Step 6: Commit**

```sh
cd /home/user/Toeshee-RMS && git reset
git add agent/internal/backupstore/
git commit -m "feat(agent): an object store that spends a prefix-scoped POST grant

Writes go out under the short-lived POST policy the control plane mints on
the dedup exchange, so the endpoint holds no object-store credential and
nothing survives the job. The form's `key` is overridden with the literal
full key rather than boto3's \${filename} placeholder, and `file` is written
last because S3 ignores every field after it.

The suffix and size checks are the cheap half, not the authoritative one --
S3 enforces the signed document -- but they stop a doomed body crossing the
wire. A 4xx is the policy's verdict and is never retried; only transport
errors are, and only because a content-addressed store makes a rewrite of
identical bytes idempotent.

https is required and redirects are refused: the grant URL is the only
address this package may talk to. Reads still refuse, naming spec §6.1's
still-open item 1, so backup.restore keeps refusing once OpenStore is
non-nil rather than failing somewhere less legible."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 5: Wire it into the running agent

**Files:**
- Modify: `agent/internal/capabilities/backup/backup.go` (add `Bind`)
- Modify: `agent/cmd/agent/main.go`
- Modify: `agent/internal/capabilities/registry.go:214-224` (the comment claiming backup cannot complete a job)
- Modify: `agent/internal/capabilities/integration_test.go`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: `(*backup.Service).Bind(o Options)`; a production agent whose `backup.run` no longer refuses.

- [ ] **Step 1: Write the failing test**

Append to `agent/internal/capabilities/backup/backup_test.go` (create the file if it does not exist; match the existing package convention in that directory):

```go
// Registration must stay a map insert: Bind wires the seams and must not dial,
// open a file, or start a goroutine.
func TestBindWiresTheSeamsAndStartsNothing(t *testing.T) {
	before := runtime.NumGoroutine()
	s := &backup.Service{}

	// Unbound: both capabilities refuse, and the refusal names what is missing.
	if _, err := s.Run(context.Background(), rawParams(t)); err == nil {
		t.Fatal("an unbound service accepted backup.run")
	}

	s.Bind(backup.Options{
		QueryChunks: func(context.Context, wire.ChunkQuery) (wire.ChunkGrant, error) {
			return wire.ChunkGrant{}, nil
		},
		OpenStore: func(context.Context, backup.Repository) (backup.ObjectStore, error) {
			return nil, nil
		},
	})
	runtime.Gosched()
	if got := runtime.NumGoroutine(); got > before {
		t.Errorf("goroutines %d -> %d; Bind started one", before, got)
	}
	if s.Runs() != 0 {
		t.Errorf("runs = %d after Bind, want 0", s.Runs())
	}
}
```

**Adapt `rawParams(t)` and the `s.Run(...)` call signature to whatever the existing backup tests use** — read `helpers_test.go` first and reuse its fixtures rather than inventing new ones.

- [ ] **Step 2: Run it to verify it fails**

```sh
cd agent && go test ./internal/capabilities/backup/ -run TestBindWires -count=1
```

Expected: FAIL — `s.Bind undefined`.

- [ ] **Step 3: Add `Bind`**

In `agent/internal/capabilities/backup/backup.go`, after `func New(o Options) *Service`:

```go
// Bind wires the process-wide Service. It is what main.go calls, and it
// follows logs.Process.Bind and provision.Process.Bind exactly: it installs
// seams and does nothing else. It opens no file, dials nothing, takes no wheel
// entry and starts no goroutine -- the capability resolves everything else at
// JOB TIME, so an idle agent that will never run a backup pays nothing for
// this call.
//
// Without it both capabilities are registered but refuse, naming the missing
// transport, which is a better answer than `unknown_type` and is one the
// control plane can act on.
func (s *Service) Bind(o Options) { s.opts = o }
```

- [ ] **Step 4: Run it to verify it passes**

```sh
cd agent && go test ./internal/capabilities/backup/ -run TestBindWires -v -count=1
```

Expected: PASS.

- [ ] **Step 5: Wire `main.go`**

In `agent/cmd/agent/main.go`, add imports for `chunkreply`, `backupstore` and `backup`, then construct the registry alongside the outbox (near line 139):

```go
	// The reply half of the backup dedup exchange. At rest this is an empty map
	// and a mutex: no goroutine, no timer, no socket.
	replies := chunkreply.New(log)
```

Bind the capability next to the other `Bind` calls (near line 218):

```go
	// `backup.run` (docs/modules/backup/spec.md §5.2). Binding gives the
	// capability its two transports and nothing else: the dedup exchange, and
	// an object store that spends the write grant that exchange returns. Both
	// resolve at JOB TIME and neither is touched while the agent is idle.
	//
	// `backup.restore` stays refused -- backupstore's Get/GetRef refuse, naming
	// prefix-scoped READ, which SigV4 cannot express (spec §6.1, item 1).
	backup.Process.Bind(backup.Options{
		QueryChunks: replies.Query,
		OpenStore:   backupstore.Open,
		Logger:      log,
	})
```

Compose the inbound handler. Replace the `transport.Run(...)`'s final argument `runner.Handle` with `inbound`, defined just above the call:

```go
	// One inbound entry point, two destinations. A chunk grant answers a
	// question a running job asked and carries no instruction, so it goes to
	// the registry; everything else is a job and goes to the runner, which
	// verifies its signature. Decoding here rather than in the runner keeps
	// `runner.Handle`'s contract "this is a job" intact.
	inbound := func(c context.Context, frame []byte) {
		if msg, err := wire.Decode(frame); err == nil {
			if g, ok := msg.(*wire.ChunkGrant); ok {
				replies.Deliver(g)
				return
			}
		}
		// Anything else -- including a frame that failed to decode -- goes to
		// the runner, which owns the logging and the refusal for it. Decoding
		// twice is deliberate: this function must not become the place that
		// decides what a job is.
		runner.Handle(c, frame)
	}
```

And add the two hooks to `transport.Options`:

```go
		OnConnected: func(c context.Context, conn *transport.Conn) error {
			// Drain what the last outage accumulated BEFORE the read loop
			// starts, so a job's answer never overtakes an older one.
			if err := outbox.Attach(c, conn); err != nil {
				return err
			}
			replies.Attach(conn)
			return nil
		},
		// A dedup grant comes back on the connection its query went out on, so
		// a pending flush must fail here rather than burn its full deadline
		// waiting for a reply that cannot arrive.
		OnDisconnect: func(_ context.Context, cause error) { replies.Detach(cause) },
```

- [ ] **Step 6: Correct the registry comment**

`agent/internal/capabilities/registry.go` currently states that `app.deploy` and both `backup` capabilities "cannot complete a real job yet" and that the fix "is server-side". Two of the three are now false. Replace the paragraph beginning `// TWO OF THESE CANNOT COMPLETE A REAL JOB YET` with:

```go
	// `backup.run` COMPLETES now. The dedup exchange (spec §5.2) returns a
	// short-lived, prefix-scoped POST grant on the agent's own mTLS channel,
	// and internal/backupstore spends it, so writes need no credential on the
	// endpoint. main.go binds both seams; unbound, the capability still
	// refuses rather than pretending.
	//
	// `backup.restore` STILL REFUSES, and names why: SigV4 query signing
	// cannot express a prefix-scoped READ, and a restore's chunk selection can
	// run to six figures, so one presigned URL each is tens of megabytes of
	// job params (spec §6.1, still-open item 1).
```

Check `app.deploy`'s status before editing its sentence — the handoff says Option 1 made it work end to end. If it does, say so; if you cannot confirm it from the code, leave its sentence alone rather than guessing.

- [ ] **Step 7: Build, vet, and run the whole agent suite**

```sh
cd agent && go build ./... && go vet ./... && go test ./... -race -count=1 2>&1 | tail -25
```

Expected: PASS across every package. Compare the count to the Task-1 baseline.

- [ ] **Step 8: Cross-compile, since the fleet is Windows and macOS**

```sh
cd agent && GOOS=windows GOARCH=amd64 go build ./... && GOOS=darwin GOARCH=arm64 go build ./... && echo CROSS_OK
```

Expected: `CROSS_OK`. This compiles for those platforms; it does not run there.

- [ ] **Step 9: Commit**

```sh
cd /home/user/Toeshee-RMS && git reset
git add agent/cmd/agent/main.go agent/internal/capabilities/backup/ agent/internal/capabilities/registry.go agent/internal/capabilities/integration_test.go
git commit -m "feat(agent): bind the backup transport, so backup.run can complete

main.go now composes one inbound entry point with two destinations -- a chunk
grant to the reply registry, everything else to the runner, which still owns
signature verification -- and binds the capability's two seams. The registry
attaches on connect and detaches on disconnect, so a pending flush fails when
its connection dies instead of waiting out the deadline.

backup.restore still refuses and still names prefix-scoped READ: the refusal
moved from a nil OpenStore into backupstore's Get/GetRef rather than
disappearing when OpenStore became non-nil.

Corrects registry.go's comment, which told the reader both backup
capabilities could not complete a job and that the fix was server-side."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 6: Derive `manifest_ref` server-side

**Files:**
- Modify: `backend/app/modules/backup/dispatch.py:234-258`
- Modify: `backend/tests/test_backup_dispatch.py`

**Interfaces:**
- Consumes: a `BackupRunResult.manifest_ref` that is now a **relative suffix** (`manifests/{name}`), produced by Task 4's `Put`.
- Produces: a `BackupPoint.manifest_ref` that is a full `s3://{bucket}/{key}` ref, which `get_backup_object`, `gc.py` and `restore.py` already consume unchanged.

There are **zero `BackupPoint` rows in existence** — `backup.run` has never completed — so this changes no stored data and needs no migration. It stops being free the moment one real backup lands.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_backup_dispatch.py` (match the file's existing fixtures and async style):

```python
async def test_success_derives_the_full_manifest_ref_from_the_suffix(
    app_engine, object_store, backup_run
):
    """The agent reports a suffix because it does not know the bucket. The
    server owns key construction -- the same derived-never-accepted rule
    `presign_backup_object` is built on."""
    await record_run_result(
        app_engine,
        backup_run,
        {"state": "success", "manifest_ref": "manifests/deadbeef", "chunk_count": 2},
    )
    point = await load_backup_point(app_engine, backup_run)
    expected = (
        f"s3://{object_store.bucket}/{backup_run.org_id}/backup/"
        f"{backup_run.repository_id}/manifests/deadbeef"
    )
    assert point.manifest_ref == expected


@pytest.mark.parametrize(
    "suffix",
    [
        "../../another-org/backup/x/manifests/m",
        "/absolute/manifests/m",
        "manifests/../../escape",
        "manifests\\windows",
        "manifests//empty-segment",
        "",
    ],
)
async def test_an_unsafe_suffix_fails_the_run_and_writes_no_point(
    app_engine, backup_run, suffix
):
    """A suffix is endpoint-supplied. Rejecting it here is what stops an
    endpoint naming another tenant's key."""
    await record_run_result(
        app_engine, backup_run, {"state": "success", "manifest_ref": suffix}
    )
    run = await load_run(app_engine, backup_run)
    point = await load_backup_point(app_engine, backup_run)
    assert (run.state, point) == ("failed", None)
    assert run.error


async def test_an_absolute_ref_from_an_older_agent_is_refused(app_engine, backup_run):
    """An agent that sends a full s3:// ref is naming a bucket, which is the
    thing the endpoint is not allowed to choose."""
    await record_run_result(
        app_engine,
        backup_run,
        {"state": "success", "manifest_ref": "s3://someone-elses-bucket/k"},
    )
    run = await load_run(app_engine, backup_run)
    assert run.state == "failed"
```

**Read the existing test file first** and reuse its fixtures and helper names (`record_run_result`, `load_run`, `load_backup_point` are placeholders for whatever it already uses). Do not introduce a parallel set of helpers.

- [ ] **Step 2: Run them to verify they fail**

```sh
cd backend && ./.venv/bin/pytest tests/test_backup_dispatch.py -k "manifest_ref or unsafe_suffix" -v
```

Expected: FAIL — the ref is stored verbatim today.

- [ ] **Step 3: Implement the derivation**

In `backend/app/modules/backup/dispatch.py`, add a module-level helper:

```python
def _resolve_manifest_ref(
    object_store: ObjectStore, org_id: UUID, repository_id: UUID, reported: str
) -> str:
    """Turn the agent's reported SUFFIX into the full object reference.

    The agent does not know the bucket and must not: a POST grant carries a
    key prefix, not a bucket name, and an endpoint that constructed a full
    reference would be naming a bucket the server then trusts. So the agent
    reports `manifests/{name}` and the key is derived here, from ids this
    process is already authorized for -- the same derived-never-accepted rule
    `presign_backup_object` is built on.

    Raises ValueError on anything that could escape the repository's prefix.
    """
    if not reported or reported.startswith(("s3://", "/", "\\")):
        raise ValueError(f"manifest_ref must be a relative suffix, got {reported!r}")
    if "\\" in reported:
        raise ValueError(f"manifest_ref contains a backslash: {reported!r}")
    for segment in reported.split("/"):
        if segment in ("", ".", ".."):
            raise ValueError(f"manifest_ref has an unsafe segment: {reported!r}")
    return object_store.backup_object_ref(org_id, repository_id, reported)
```

If `ObjectStore` has no `backup_object_ref`, add it next to `_backup_key` in `backend/app/core/storage.py`:

```python
    def backup_object_ref(self, org_id: UUID, repository_id: UUID, suffix: str) -> str:
        """The full `s3://` reference for a repository-relative suffix.

        The read side of `put_backup_object`'s return value, so the two cannot
        drift: `get_backup_object` is the only consumer of either.
        """
        return f"s3://{self._bucket}/{self._backup_key(org_id, repository_id, suffix)}"
```

Then in the result handler (around `dispatch.py:234`), replace the direct use of `result.manifest_ref` with the resolved value, failing the run on `ValueError` the same way a missing `manifest_ref` already fails it:

```python
    if result.state == "success" and result.manifest_ref:
        try:
            manifest_ref = _resolve_manifest_ref(
                object_store, run.org_id, run.repository_id, result.manifest_ref
            )
        except ValueError as exc:
            run.state = "failed"
            run.error = f"agent reported an unusable manifest_ref: {exc}"
        else:
            ...  # existing BackupPoint creation, with manifest_ref=manifest_ref
```

**Read the surrounding function before editing** and keep its existing control flow, transaction handling and audit calls intact.

- [ ] **Step 4: Run them to verify they pass**

```sh
cd backend && ./.venv/bin/pytest tests/test_backup_dispatch.py -v 2>&1 | tail -20
```

Expected: PASS, including every pre-existing test in the file.

- [ ] **Step 5: Update `Put`'s contract comment in the agent**

`ObjectStore.Put`'s doc comment in `agent/internal/capabilities/backup/backup.go` says it returns "the reference THE CONTROL PLANE will use to fetch it again". That is no longer true. Replace that sentence:

```go
	// Put stores data at suffix UNDER A SERVER-ISSUED GRANT and returns the
	// SUFFIX it wrote.
	//
	// Not a full reference: the agent does not know the bucket and must not.
	// A POST grant carries a key prefix, never a bucket name, and an endpoint
	// that constructed a full reference would be naming a bucket the server
	// then trusts. The suffix travels back as `manifest_ref`, and
	// `dispatch.py::_resolve_manifest_ref` derives the real object reference
	// from ids the control plane is already authorized for.
```

- [ ] **Step 6: Run the typecheck, linter and the backup suites**

```sh
cd backend && ./.venv/bin/mypy app && ./.venv/bin/ruff check app tests && ./.venv/bin/pytest tests/ -k backup -q 2>&1 | tail -15
cd ../agent && go build ./... && go test ./internal/capabilities/backup/ -count=1
```

Expected: clean mypy, clean ruff, PASS on both suites.

- [ ] **Step 7: Falsify the suffix guard**

Temporarily make `_resolve_manifest_ref` skip its checks (`return object_store.backup_object_ref(...)` as the first statement). **Confirm the edit applied**, then:

```sh
cd backend && ./.venv/bin/pytest tests/test_backup_dispatch.py -k unsafe_suffix -q
```

Expected: FAIL on the traversal cases. Revert and re-run to confirm PASS.

- [ ] **Step 8: Commit**

```sh
cd /home/user/Toeshee-RMS && git reset
git add backend/app/modules/backup/dispatch.py backend/app/core/storage.py backend/tests/test_backup_dispatch.py agent/internal/capabilities/backup/backup.go
git commit -m "feat(backup): derive manifest_ref from the suffix the agent reports

get_backup_object needs s3://{bucket}/{key} and the agent contractually does
not know the bucket -- a POST grant carries a key prefix, never a bucket name
-- so there was no correct value for Put to return. The agent now reports the
relative suffix and the server derives the key from ids it is already
authorized for, the same derived-never-accepted rule presign_backup_object is
built on. An endpoint that names a bucket, an absolute path or a traversal
fails the run and writes no backup point.

Free to do now and not later: backup.run has never completed, so there are
zero BackupPoint rows and this migrates no data."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 7: Prove the idle budget and the footprint survived

The transport seams are real now, where they used to be injected fakes. The properties `budget_test.go` guards must still hold.

**Files:**
- Modify: `agent/internal/capabilities/backup/budget_test.go`

- [ ] **Step 1: Write the failing test**

Append to `agent/internal/capabilities/backup/budget_test.go`:

```go
// `go test -bench` EXITS 0 ON A REGRESSION -- measured on this project: a
// planted 64 KiB/op regression printed `65536 B/op` and passed. So a budget
// assertion has to be an ordinary Test that calls testing.Benchmark and
// compares, never a Benchmark func.
//
// And it gates on CUMULATIVE ALLOCATION, not on RSS or retained heap: unbounded
// buffering is transient by construction, freed before the handler returns, so
// an after-the-fact reading cannot see the peak it exists to catch.
func TestDedupAllocationDoesNotScaleWithChunkCount(t *testing.T) {
	measure := func(chunks int) uint64 {
		res := testing.Benchmark(func(b *testing.B) {
			for i := 0; i < b.N; i++ {
				runDedupSession(b, chunks) // helper in this package
			}
		})
		return res.AllocedBytesPerOp()
	}

	small := measure(64)
	large := measure(1024)

	// 16x the chunks. A per-chunk REFERENCE is ~100 bytes and a batch is
	// bounded at 1024, so allocation may grow -- but nothing may hold chunk
	// PLAINTEXT, which is what a superlinear jump would mean.
	if large > small*24 {
		t.Errorf("allocation %d -> %d B/op for 64 -> 1024 chunks; "+
			"that is faster than the 16x growth in references, so something is "+
			"buffering payload", small, large)
	}
	t.Logf("alloc/op: 64 chunks = %d, 1024 chunks = %d", small, large)
}
```

**Adapt `runDedupSession` to the helpers already in this package** — read `budget_test.go` and `helpers_test.go` first and reuse the existing fixture that drives a dedup session against a fake `QueryChunks`. Do not build a second fixture.

- [ ] **Step 2: Run it**

```sh
cd agent && go test ./internal/capabilities/backup/ -run TestDedupAllocationDoesNotScale -v -count=1
```

Expected: PASS, with the two numbers logged. Record them in the task report — the numbers are the result, not the pass.

- [ ] **Step 3: Falsify it**

Temporarily make `pendingChunk` carry the payload: add a `data []byte` field and populate it where pending entries are appended in `dedup.go`. **Confirm the edit applied**, then re-run.

Expected: FAIL, with `large` far more than 24× `small`. This is the check that `budget_test.go`'s falsifier 3 still bites now that a real transport is behind the seam. Revert and re-run to confirm PASS.

- [ ] **Step 4: Confirm the whole agent is still idle at rest**

```sh
cd agent && go test ./... -race -count=1 2>&1 | tail -25
```

Expected: PASS everywhere, including any existing dormancy assertions.

- [ ] **Step 5: Commit**

```sh
cd /home/user/Toeshee-RMS && git reset
git add agent/internal/capabilities/backup/budget_test.go
git commit -m "test(agent/backup): gate dedup allocation now that the seam is real

The footprint property -- a pending entry is a ~100-byte reference, never
chunk plaintext -- was proven against an injected fake. A real transport now
sits behind that seam, so the gate is re-run against it.

Built as an ordinary Test calling testing.Benchmark: go test -bench exits 0
on a regression, measured here. Gates on cumulative allocation, because an
after-the-fact RSS reading cannot see a transient peak."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 8: The live measurement, and telling the truth about it

This is the bar: writes are solved on paper until something measures them. `moto` cannot do it — it accepts an upload whose key violates the signed policy and answers `204`, measured rather than assumed.

**Files:**
- Create: `scripts/verify_backup_run.py`
- Modify: `docs/modules/backup/spec.md` §6.1
- Modify: `HANDOFF.md` §2, §5, §6

- [ ] **Step 1: Bring up the stack**

```sh
cd deploy && docker compose up -d && docker compose ps
```

Expected: every service healthy. If Traefik answers a bare 404 on everything while containers look healthy, the Docker Engine is too old — use Docker's own apt repo, not Ubuntu's `docker.io` (HANDOFF.md §3).

- [ ] **Step 2: Write the verification script**

Create `scripts/verify_backup_run.py`, modelled on `scripts/smoke_site_server.py` (564 lines — read it first for the house style, argument handling and step/reporting helpers).

It must, against the **real** stack and a **real** compiled agent binary:

1. Create an org, enroll a device, create a backup repository, and seed a small source tree with a known number of distinct chunks.
2. Dispatch a real signed `backup.run` and wait for its result.
3. Assert the exchange happened: at least one `backup_chunk_query` was answered, and `backup_chunks` rows exist for the repository.
4. Assert the objects landed: chunk keys exist under `{org_id}/backup/{repository_id}/chunks/`, and the manifest under `.../manifests/`.
5. Assert the `BackupPoint` row exists and `get_backup_object(point.manifest_ref)` returns the sealed manifest.
6. **The two negatives `moto` cannot express**, driven directly against MinIO with a grant minted by `presign_backup_write`:
   - a POST whose `key` is outside `{storage_ref}/` → **403**
   - a POST whose body exceeds `content-length-range` → **400**
7. **Run it a second time over unchanged data** → assert the new-chunk count is near zero. Without this, every assertion above passes on an exchange that dedups nothing.
8. Print a plain report of what was and was not driven end to end.

Each step prints pass/fail and the script exits non-zero on any failure.

- [ ] **Step 3: Run it**

```sh
cd /home/user/Toeshee-RMS && backend/.venv/bin/python scripts/verify_backup_run.py
```

Expected: every step passes. **If a step cannot be driven end to end, say so in the report and in the task summary — do not substitute a mock.** A step that was skipped is a finding, not a footnote.

- [ ] **Step 4: Amend spec §6.1**

Rewrite the amendment note at `docs/modules/backup/spec.md:261-267` and the "Still open after §5.2" list at `:332-347`. Items 2 and 3 are closed; item 1 is not. State what was measured, by what, on which platform, and what remains unproven — mirroring the honesty of §5.5 in the design doc. Do **not** claim CI coverage: there is none.

- [ ] **Step 5: Update `HANDOFF.md`**

- **§2** — Option 7's "WRITES DONE, RESTORE NOT" becomes accurate about what now runs end to end; delete "Confirm whether `backup.run` now completes end to end", which is answered.
- **§5** — remove "`backup.run` does not work end to end"; add what the live run measured and, explicitly, that no Windows or macOS endpoint has run any of it.
- **§6** — drop "Verify `backup.run` end to end" and the `internal/wire/schema_test.go` row (both done). Keep "Prefix-scoped READ" as the top item and keep Option 4 as its strongest candidate.

- [ ] **Step 6: Full verification against the baseline**

```sh
cd backend && ./.venv/bin/pytest -q 2>&1 | tail -5
cd ../backend && ./.venv/bin/mypy app && ./.venv/bin/ruff check app tests
cd ../agent && go test ./... -race -count=1 2>&1 | tail -5
cd ../frontend && npx vitest run 2>&1 | tail -5
```

Expected: `~5830 passed` (backend), clean mypy and ruff, PASS across the agent, `491 passed` (frontend). **Report the actual numbers against those baselines.** A drop is a regression to investigate, not a rounding error.

- [ ] **Step 7: Commit**

```sh
cd /home/user/Toeshee-RMS && git reset
git add scripts/verify_backup_run.py docs/modules/backup/spec.md HANDOFF.md
git commit -m "test(backup): measure backup.run end to end against real MinIO

Writes were solved on paper. This drives a real signed job through a real
agent binary to real MinIO behind real Traefik: the dedup exchange crosses
the wire, chunks and the manifest land under the repository prefix, and the
BackupPoint's manifest_ref is fetchable.

It also drives the two negatives moto cannot express -- it accepts a
policy-violating key and answers 204, measured rather than assumed -- so a
key outside the prefix must be 403 and an oversized body 400 against a real
object store. And it runs twice over unchanged data, because every other
assertion here passes on an exchange that dedups nothing.

Amends spec §6.1 and HANDOFF.md: items 2 and 3 closed, item 1 open, and no
CI guards any of it."
git push -u origin claude/handoff-spec-review-f45idd
```

---

## Self-Review

**Spec coverage.** §1.1 correlation → Task 3 (registry, and the `OnDisconnect` hook the spec's §1.1 addendum requires). §1.2 types move to `wire` → Tasks 1 and 2. §1.3 server-derived `manifest_ref` → Task 6. §1.4 system trust roots, https-only, no redirects → Task 4. §1.5 restore keeps refusing → Task 4 (`Get`/`GetRef`) and Task 5 (registry comment). §2 components → Tasks 1–6, one per unit. §3 data flow → exercised whole in Task 8. §4 error handling: silence/deadline → Task 3; disconnect → Task 3; unknown and duplicate grants → Task 3; `new ⊄ check` → **already implemented and tested in `dedup.go:204`**, which is why no task adds it; grant expiry, prefix and size rejections → Task 4 and Task 8's negatives; retry policy → Task 4; re-read mismatch → pre-existing; GC repair → pre-existing. §5.1 unit tests → Tasks 1, 3, 4, 6. §5.2 budget tests → Task 7. §5.3 fixture blindness → falsification steps in Tasks 1, 3, 4, 6, 7. §5.4 live measurement → Task 8. §5.5 stated limits → Task 8 steps 4 and 5.

**One spec item deliberately has no task.** §4's `new ⊄ check` check already exists at `dedup.go:204` and is covered by the backup suite. Adding it again would be a second, divergent guard — the "two redundant guards mask each other" trap in `HANDOFF.md` §4.

**Placeholder scan.** No "TBD" or "handle edge cases". Four steps say *"adapt to the existing helpers"* (Task 3 step 6, Task 5 step 1, Task 6 step 1, Task 7 step 1) rather than inventing fixture names for files I did not fully read; each names the file to read first and forbids introducing a parallel fixture set. That is a deliberate instruction, not a gap.

**Type consistency.** `wire.ChunkQuery`/`ChunkGrant`/`UploadGrant` are defined in Task 1 and used under those names in Tasks 2–5. `QueryChunks` keeps its name and gains `wire.` qualifiers in Task 2, matching `chunkreply.Query`'s signature in Task 3. `ObjectStore.Put(ctx, wire.UploadGrant, string, []byte) (string, error)` in Task 2 matches `backupstore`'s implementation in Task 4 and `Put`'s revised contract in Task 6. `Options.OnDisconnect func(context.Context, error)` in Task 3 matches `main.go`'s hook in Task 5. `backup.Options{QueryChunks, OpenStore, Logger}` in Task 5 uses only fields that exist in `Options` today.

**Known risk this plan does not remove.** The live script proves the path on **Linux**, against a compose certificate that is not the operator's real one. The fleet is Windows and macOS, and nothing here changes that.

**Corrected after the branch review:** this paragraph originally also claimed "CI has no Go toolchain". That was false, and it was inherited unchecked from text written while the agent lived in a separate repository. `.github/workflows/ci.yml` has four jobs, and the `agent` job pins Go 1.24 and runs `gofmt`, `go vet`, `staticcheck`, `go test ./...`, the idle-budget tests, cross-compiles and packaging on every push. What genuinely has no CI is `scripts/verify_backup_run.py` — the live MinIO measurement — which is manual and Linux-only.

# Prefix-Scoped READ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `backup.restore` work — today `backupstore.Get` and `GetRef` both refuse, so the module takes backups and cannot return them.

**Architecture:** A new wire kind pair (`backup_read_query` / `backup_read_grant`) carries batches of ≤256 object **suffixes** to the control plane, which signs a presigned GET per suffix under the job's own `{storage_ref}/` prefix and nowhere else. The agent fetches bytes directly from the object store, exactly as writes already do. A `readreply` registry mirrors `chunkreply` for reply correlation; a `Expect` lookahead seam lets the store ask for 256 grants at a time while restore keeps streaming one chunk at a time.

**Tech Stack:** Go 1.24 (agent), Python 3.12 / FastAPI / SQLAlchemy 2.x (control plane), botocore presigning. No new dependency on either side.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-13-prefix-scoped-read-design.md` (commit `d82b700`). Where this plan and the spec disagree, the spec governs — **raise it, do not choose**. Three deliberate reconciliations are recorded below; everything else follows the spec.
- **The query carries SUFFIXES, never full keys.** The server joins each onto the repository prefix itself, so an agent cannot express a key outside its own repository.
- **The server refuses the WHOLE batch** if any suffix is unsafe. It never signs the rest.
- `org_id` and `device_id` come from the **gateway's mTLS binding**; `repository_id` and `storage_ref` come from the **job record**. Never from the request payload.
- Batch size **256** (`READ_BATCH_MAX`); grant expiry **900s** (`READ_GRANT_TTL`).
- Reads are idempotent: an expired URL, a dropped correlation, or a transient store error all **re-request**, bounded at 3 attempts. A read that keeps failing fails the restore with a stated reason.
- Fetch is **sequential**. Parallelism is a separate cycle with its own budget falsifier.
- Transport policy is the write path's, unchanged: https only, system trust roots, plaintext refused, redirects refused.
- No refusal reason, log line, or audit record may contain an object key or a customer path.
- Never `git add -A`, `git add .`, `git commit -a`, `git checkout .`, or `git stash`. Stage named paths only.
- No model identifier or `Co-Authored-By` trailer in commit messages.
- After a Go task: `cd agent && gofmt -l . && go vet ./... && go test ./... -count=1`.
  After a Python task: `cd backend && ruff check . && mypy app && pytest -q`.

### Three reconciliations with the existing codebase

These are places where the spec's shape and the shipped code's conventions
collide. Each is resolved here in the codebase's favour, and each is called out
so a reviewer can overrule rather than discover it.

1. **`query_id`, not `request_id`.** Spec §3 names the correlation token
   `request_id`. `BackupChunkQueryMsg` and `chunkreply` already call the
   identical thing `query_id`. Two names for one concept across two halves of
   one exchange is a drift generator; this plan uses `query_id` throughout.

2. **No `refused` field. Every refusal is silence plus an audit event.**
   Spec §3 gives the grant a `refused: "<stated reason>"` variant.
   `BackupChunkGrantMsg`'s docstring records the opposite decision, explicitly:
   *"There is deliberately NO error variant … A reply that said 'no such job'
   and a reply that said 'not your job' would together be an oracle a
   compromised endpoint could walk."* The read query answers exactly the same
   authorization questions, so it inherits exactly the same rule. What replaces
   the `refused` variant is **layered wire-level validation** — the convention
   `protocol.py` already states for chunk ids (*"Rejected at the wire, again by
   the `backup_chunks` CHECK, and again by `_safe_object_suffix` — none of the
   three is the only layer"*): an unsafe suffix or an over-cap batch is rejected
   by the Pydantic model before any handler sees it. The agent's failure mode
   for every refusal is therefore `readreply`'s bounded timeout, carrying the
   same "this is what a refused job looks like" message `chunkreply` already
   emits.

3. **`GetRef` takes a full `s3://bucket/…` reference, and the agent derives the
   suffix.** Spec §3 says the manifest needs no special path, which holds — but
   it assumes the agent already has a suffix. It does not:
   `backup.ObjectStore.GetRef`'s contract is a full server-issued reference,
   because `dispatch.py::_resolve_manifest_ref` builds one. Task 6 derives the
   repository-relative suffix from that reference inside `GetRef` and then goes
   through the same one-suffix query as any chunk. The spec's falsifier — *"a
   `GetRef` that cannot be served by a one-suffix query"* — stays satisfied.

---

## File Structure

| File | Responsibility |
|---|---|
| `agent/internal/wire/wire.go` | the two new kinds; `ReadQuery` / `ReadGrant` / `ReadGrantItem`; `Decode` returning `*ReadGrant` |
| `agent/internal/readreply/readreply.go` | reply correlation for read grants; a deliberate duplicate of `chunkreply` |
| `agent/internal/backupstore/backupstore.go` | `Get`/`GetRef` against batched grants; the windowed batch cache |
| `agent/internal/capabilities/backup/backup.go` | `Repository.JobID`; the `BatchReader` lookahead seam |
| `agent/internal/capabilities/backup/restore.go` | announce the fetch order so the store can batch |
| `agent/cmd/agent/main.go` | construct the registry, route `*ReadGrant`, bind the store's granter |
| `backend/app/gateway/protocol.py` | `BackupReadQueryMsg` / `BackupReadGrantMsg`; the suffix pattern and batch cap |
| `backend/app/core/storage.py` | `presign_backup_read` — the batch signing half |
| `backend/app/modules/backup/read.py` | `answer_read_query` / `handle_read_query` |
| `backend/app/gateway/dispatch.py` | route the inbound kind |
| `scripts/verify_backup_run.py` | the restore leg of the live measurement |

---

### Task 1: The wire kinds and types

**Files:**
- Modify: `agent/internal/wire/wire.go`
- Test: `agent/internal/wire/wire_test.go`

**Interfaces:**
- Produces: `KindBackupReadQuery = "backup_read_query"`, `KindBackupReadGrant = "backup_read_grant"`,
  `type ReadQuery struct{ JobID, QueryID string; Suffixes []string }`,
  `type ReadGrantItem struct{ Suffix, URL string }`,
  `type ReadGrant struct{ JobID, QueryID string; Grants []ReadGrantItem; ExpiresAt string }`.
  `Decode` returns `*ReadGrant` for `KindBackupReadGrant`.

- [ ] **Step 1: Write the failing tests**

Append to `agent/internal/wire/wire_test.go`:

```go
func TestDecodeReturnsAReadGrantForTheReadKind(t *testing.T) {
	frame := []byte(`{"v":1,"kind":"backup_read_grant","job_id":"j1","query_id":"q1",` +
		`"grants":[{"suffix":"chunks/ab/abcd","url":"https://store.example/o?sig=x"}],` +
		`"expires_at":"2026-08-13T10:00:00Z"}`)
	msg, err := Decode(frame)
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}
	g, ok := msg.(*ReadGrant)
	if !ok {
		t.Fatalf("Decode returned %T, want *ReadGrant", msg)
	}
	if g.QueryID != "q1" || len(g.Grants) != 1 || g.Grants[0].Suffix != "chunks/ab/abcd" {
		t.Fatalf("decoded = %+v, want one grant for chunks/ab/abcd with query_id q1", g)
	}
}

func TestAReadQueryEncodesAsAnOutboundFrame(t *testing.T) {
	// THROUGH Encode, not json.Marshal. Encode frames only values that
	// implement the unexported `outbound` interface, so a ReadQuery without a
	// kind() method cannot be sent at all -- and a json.Marshal test would
	// pass anyway, proving nothing about the path readreply actually uses.
	//
	// The `suffixes` name is the server's Pydantic field; a mismatch is a
	// silent parse failure on the control plane, which answers with silence.
	b, err := Encode(ReadQuery{JobID: "j1", QueryID: "r1", Suffixes: []string{"chunks/ab/abcd"}})
	if err != nil {
		t.Fatalf("Encode(ReadQuery): %v", err)
	}
	got := string(b)
	if !strings.Contains(got, `"kind":"backup_read_query"`) {
		t.Fatalf("encoded = %s, want kind backup_read_query", got)
	}
	if !strings.Contains(got, `"suffixes":["chunks/ab/abcd"]`) {
		t.Fatalf("encoded = %s, want a `suffixes` array", got)
	}
}
```

Add `"strings"` to the test file's imports if it is not already there.

- [ ] **Step 2: Run it and watch it fail**

Run: `cd agent && go test ./internal/wire/ -run 'Read' -count=1`
Expected: FAIL — `undefined: ReadGrant`.

- [ ] **Step 3: Add the kinds and types**

Beside the existing `KindBackupChunkQuery` / `KindBackupChunkGrant` constants:

```go
	// The READ half of the repository exchange, added because SigV4 query
	// signing cannot express a prefix-scoped READ and a restore's selection
	// can run to six figures (docs/modules/backup/spec.md §6.1 item 1).
	//
	// A SEPARATE kind pair rather than a mode field on the chunk query:
	// inbound parsing is deliberately loose, so an OLD control plane would
	// ignore a discriminator and answer with a WRITE grant, which a new agent
	// could misread as authorization it does not hold. An unknown kind fails
	// loudly instead -- the old server rejects the frame, nothing answers, and
	// readreply's bounded wait ends the restore with a stated reason.
	KindBackupReadQuery = "backup_read_query"
	KindBackupReadGrant = "backup_read_grant"
```

Beside the `ChunkQuery` / `ChunkGrant` types:

```go
// ReadQuery asks the control plane to sign a batch of object reads.
//
// Suffixes are RELATIVE to the repository prefix and are never absolute keys:
// the server joins each onto the prefix it derives from the job, so this agent
// cannot express a location outside its own repository. The boundary is a
// property of the construction rather than of remembering to validate.
type ReadQuery struct {
	JobID   string `json:"job_id"`
	QueryID string `json:"query_id"`
	// Suffixes is bounded at 256 by the server's parse layer; a larger batch
	// is rejected before any handler sees it.
	Suffixes []string `json:"suffixes"`
}

// ReadGrantItem is one signed GET, for one suffix.
type ReadGrantItem struct {
	Suffix string `json:"suffix"`
	URL    string `json:"url"`
}

// ReadGrant is the reply.
//
// There is deliberately NO error variant, for the reason BackupChunkGrantMsg
// states on the write side: a reply that distinguished "no such job" from "not
// your job" would be an oracle a compromised endpoint could walk. Every
// authorization refusal is silence plus a server-side audit record, and the
// agent sees it as readreply's bounded timeout.
type ReadGrant struct {
	JobID     string          `json:"job_id"`
	QueryID   string          `json:"query_id"`
	Grants    []ReadGrantItem `json:"grants"`
	ExpiresAt string          `json:"expires_at"`
}
```

In `Decode`, beside the existing `KindBackupChunkGrant` branch:

```go
	if *f.Kind == KindBackupReadGrant {
		var g ReadGrant
		if err := json.Unmarshal(frame, &g); err != nil {
			return nil, fmt.Errorf("wire: undecodable backup_read_grant: %w", err)
		}
		return &g, nil
	}
```

- [ ] **Step 4: Run the tests**

Run: `cd agent && go test ./internal/wire/ -count=1`
Expected: PASS, including the pre-existing chunk-grant tests.

- [ ] **Step 5: Commit**

```bash
git add agent/internal/wire/wire.go agent/internal/wire/wire_test.go
git commit -m "feat(agent/wire): the backup read query and grant kinds"
```

---

### Task 2: The `readreply` correlation registry

**Files:**
- Create: `agent/internal/readreply/readreply.go`
- Test: `agent/internal/readreply/readreply_test.go`

**Interfaces:**
- Consumes: `wire.ReadQuery`, `wire.ReadGrant` (Task 1).
- Produces: `func New(log *slog.Logger) *Registry`; methods `Attach(Sender)`, `Detach(cause error)`, `Deliver(*wire.ReadGrant) bool`, `Pending() int`, `Query(ctx context.Context, q wire.ReadQuery) (wire.ReadGrant, error)`; `type Sender interface{ Send(ctx context.Context, msg any) error }`; sentinels `ErrNoConnection`, `ErrDisconnected`, `ErrTimeout`, `ErrTooManyPending`, `ErrDuplicateQuery`; `const DefaultDeadline = 60 * time.Second`.

**Read first:** `agent/internal/chunkreply/chunkreply.go`. This registry is the
same shape with a different payload type, and it is **duplicated rather than
generalised** — per the convention at
`internal/capabilities/netsweep/netsweep_test.go:1240`, and because a regression
in a shared registry would break `backup.run`, not merely restore.

- [ ] **Step 1: Write the failing tests**

Create `agent/internal/readreply/readreply_test.go`:

```go
package readreply

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

type fakeSender struct{ sent []wire.ReadQuery }

func (f *fakeSender) Send(_ context.Context, msg any) error {
	q, ok := msg.(wire.ReadQuery)
	if !ok {
		return errors.New("unexpected message type")
	}
	f.sent = append(f.sent, q)
	return nil
}

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestAQueryIsAnsweredByItsOwnGrant(t *testing.T) {
	r := New(quiet())
	s := &fakeSender{}
	r.Attach(s)

	done := make(chan wire.ReadGrant, 1)
	go func() {
		g, err := r.Query(context.Background(), wire.ReadQuery{
			JobID: "j1", QueryID: "q1", Suffixes: []string{"chunks/ab/abcd"},
		})
		if err != nil {
			t.Errorf("Query: %v", err)
		}
		done <- g
	}()

	waitForPending(t, r, 1)
	if !r.Deliver(&wire.ReadGrant{JobID: "j1", QueryID: "q1",
		Grants: []wire.ReadGrantItem{{Suffix: "chunks/ab/abcd", URL: "https://x"}}}) {
		t.Fatal("Deliver reported no waiter for a query that is pending")
	}
	if g := <-done; len(g.Grants) != 1 {
		t.Fatalf("answered with %+v, want one grant", g)
	}
}

func TestAGrantForAnotherJobDoesNotAnswerThisOne(t *testing.T) {
	// The key is (job_id, query_id): query_id restarts at "r1" for every job,
	// so a late grant from a finished job must not satisfy a live one.
	r := New(quiet())
	r.Attach(&fakeSender{})

	go func() {
		_, _ = r.Query(context.Background(), wire.ReadQuery{JobID: "j2", QueryID: "q1"})
	}()
	waitForPending(t, r, 1)

	if r.Deliver(&wire.ReadGrant{JobID: "j1", QueryID: "q1"}) {
		t.Fatal("a grant for job j1 answered a query from job j2")
	}
}

func TestADisconnectFailsEveryPendingWait(t *testing.T) {
	// A restore can run for hours; a reconnect must not leave a fetch blocked
	// until its deadline waiting for a reply that can no longer arrive.
	r := New(quiet())
	r.Attach(&fakeSender{})

	errc := make(chan error, 1)
	go func() {
		_, err := r.Query(context.Background(), wire.ReadQuery{JobID: "j1", QueryID: "q1"})
		errc <- err
	}()
	waitForPending(t, r, 1)

	r.Detach(errors.New("gateway disconnected"))
	select {
	case err := <-errc:
		if !errors.Is(err, ErrDisconnected) {
			t.Fatalf("pending Query after Detach = %v, want ErrDisconnected", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("a pending Query did not return within 2s of Detach")
	}
}

func TestQueryingWithNoGatewayAttachedFailsRatherThanBlocking(t *testing.T) {
	r := New(quiet())
	_, err := r.Query(context.Background(), wire.ReadQuery{JobID: "j1", QueryID: "q1"})
	if !errors.Is(err, ErrNoConnection) {
		t.Fatalf("Query with no sender attached = %v, want ErrNoConnection", err)
	}
}

func TestASilentControlPlaneEndsTheWaitWithANamedTimeout(t *testing.T) {
	// Every authorization refusal is silence (wire.ReadGrant's comment). If the
	// wait were unbounded, a refused restore would look hung forever.
	r := New(quiet())
	r.Deadline = 50 * time.Millisecond
	r.Attach(&fakeSender{})

	start := time.Now()
	_, err := r.Query(context.Background(), wire.ReadQuery{JobID: "j1", QueryID: "q1"})
	if !errors.Is(err, ErrTimeout) {
		t.Fatalf("Query against a silent server = %v, want ErrTimeout", err)
	}
	if d := time.Since(start); d > 2*time.Second {
		t.Fatalf("the wait took %s; it must be bounded by Deadline", d)
	}
}

func waitForPending(t *testing.T, r *Registry, n int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if r.Pending() == n {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("waited 2s for %d pending queries, have %d", n, r.Pending())
}
```

- [ ] **Step 2: Run and watch it fail**

Run: `cd agent && go test ./internal/readreply/ -count=1`
Expected: FAIL — the package does not exist yet.

- [ ] **Step 3: Implement, mirroring `chunkreply` line for line**

Copy `agent/internal/chunkreply/chunkreply.go` to
`agent/internal/readreply/readreply.go` and change exactly these things:

1. `package chunkreply` → `package readreply`, and every `chunkreply:` prefix in
   an error string → `readreply:`.
2. `*wire.ChunkGrant` → `*wire.ReadGrant`, `wire.ChunkQuery` → `wire.ReadQuery`,
   `wire.ChunkGrant{}` → `wire.ReadGrant{}`.
3. `maxPending`: keep 16, and update its comment — a restore fetches serially,
   so the realistic count is 1, and the cap turns a bug that spawned unbounded
   batches into a named failure rather than quiet memory growth.
4. `Query`'s doc comment: it satisfies `backupstore.ReadGranter`, not
   `backup.QueryChunks`.
5. The package comment. Keep the four sections and rewrite them for reads:

```go
// Package readreply correlates one inbound backup_read_grant to the fetch that
// asked for it.
//
// # Why this is a duplicate of chunkreply and not a generalisation of it
//
// The two registries are the same shape over different payloads. Merging them
// behind a type parameter would put backup.run and backup.restore on one piece
// of code, so a regression in the shared half would break the capability that
// TAKES backups, not merely the one that returns them. The write path is
// measured and proven; this one is new. The convention is stated at
// internal/capabilities/netsweep/netsweep_test.go:1240.
//
// # Why silence is the hard part
//
// The control plane answers an unauthorized read query with NOTHING, for the
// reason BackupChunkGrantMsg states: a distinguishable error would be an oracle
// a compromised endpoint could walk. So a wait here MUST be bounded, or a
// restore whose job the server refuses hangs forever instead of failing where
// an operator can see it.
//
// # The idle budget
//
// A registry at rest is an empty map and a mutex. No goroutine, no timer, no
// socket, no allocation until a query is in flight.
```

6. **Every operator-facing string that names the write path.** `chunkreply`'s
   log lines and error texts say "dedup", "chunk", "run" and "flush", and a
   verbatim copy tells an operator whose RESTORE timed out that the control
   plane refused an unauthorized *dedup query*. Retarget all of them:
   `Detach`'s "backup dedup queries in flight", `Deliver`'s "dropped a backup
   chunk grant", the `key` comment crediting `dedup.go` with minting the query
   id (here it is `backupstore`, minting `r1`, `r2`, … per restore), and the
   timeout error's "unauthorized dedup query". Do NOT change the package
   comment's references to `chunkreply` and `BackupChunkGrantMsg` — those are
   deliberate cross-references, not drift.

The mechanics — the `(job, query)` key, `Attach`/`Detach`/`Deliver`/`Pending`,
the buffered channel, the `forget` on every exit path, the bounded deadline —
carry across unchanged. Those are the properties the tests above pin.

- [ ] **Step 4: Run the tests, including under `-race`**

Run: `cd agent && go test ./internal/readreply/ -count=1 -race`
Expected: PASS, 5 tests, no race.

- [ ] **Step 5: Commit**

```bash
git add agent/internal/readreply/
git commit -m "feat(agent): a reply registry for read grants, mirroring chunkreply"
```

---

### Task 3: The control plane learns the two kinds

**Files:**
- Modify: `backend/app/gateway/protocol.py`
- Test: `backend/tests/test_gateway_protocol.py` (extend; create if absent)

**Interfaces:**
- Produces: `READ_BATCH_MAX = 256`, `ObjectSuffix`,
  `class BackupReadQueryMsg(_Msg)` with `kind: Literal["backup_read_query"]`,
  `job_id: str`, `query_id: str`, `suffixes: list[ObjectSuffix]`;
  `class BackupReadGrantMsg(_Msg)` with `kind`, `job_id`, `query_id`,
  `grants: list[BackupReadGrantItem]`, `expires_at: str`;
  `class BackupReadGrantItem(BaseModel)` with `suffix: str`, `url: str`.
  `BackupReadQueryMsg` is added to the `AgentMessage` union.

**Read first:** `BackupChunkQueryMsg` and `BackupChunkGrantMsg` in the same
file, and the `CHUNK_ID_PATTERN` / `MAX_CHUNK_BATCH` block above them. This task
is the same three moves — a validated item type, a batch cap, a discriminated
member — applied to suffixes.

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from pydantic import ValidationError

from app.gateway.protocol import READ_BATCH_MAX, BackupReadQueryMsg, parse_agent_message


def _frame(suffixes: list[str]) -> str:
    import json

    return json.dumps(
        {"v": 1, "kind": "backup_read_query", "job_id": "j1", "query_id": "q1",
         "suffixes": suffixes}
    )


def test_a_read_query_parses_into_its_own_type() -> None:
    msg = parse_agent_message(_frame(["chunks/ab/abcd", "manifests/m1"]))
    assert isinstance(msg, BackupReadQueryMsg)
    assert msg.suffixes == ["chunks/ab/abcd", "manifests/m1"]


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "chunks/../../../secrets",
        "/absolute/key",
        "chunks\\ab\\abcd",
        "chunks//ab",
        "chunks/ab/",
        "chunks/ab/ab\x00cd",
        "chunks/ab/ab\tcd",
        "",
    ],
)
def test_an_unsafe_suffix_is_rejected_at_the_wire(bad: str) -> None:
    """The FIRST of three layers, not the only one.

    `presign_backup_read` validates again with `_safe_object_suffix`. Rejecting
    here means a hostile suffix never reaches a handler at all, so the refusal
    is the same silence every other unauthorized query gets.
    """
    with pytest.raises(ValidationError):
        parse_agent_message(_frame(["chunks/ab/abcd", bad]))


def test_a_batch_over_the_cap_is_rejected_at_the_wire() -> None:
    over = [f"chunks/ab/{i:04x}" for i in range(READ_BATCH_MAX + 1)]
    with pytest.raises(ValidationError):
        parse_agent_message(_frame(over))
    # ... and the cap itself parses, so the boundary is the stated one.
    assert len(parse_agent_message(_frame(over[:READ_BATCH_MAX])).suffixes) == READ_BATCH_MAX
```

- [ ] **Step 2: Run and watch it fail**

Run: `cd backend && pytest tests/test_gateway_protocol.py -q -k read`
Expected: FAIL — `ImportError: cannot import name 'BackupReadQueryMsg'`.

- [ ] **Step 3: Implement**

Add below the `BackupChunkGrantMsg` class:

```python
# --------------------------------------------------------------------------- #
# Backup READ exchange (docs/superpowers/specs/2026-08-13-prefix-scoped-read-
# design.md). The write half above authorizes a PREFIX with one POST policy;
# SigV4 query signing cannot express that for a READ, so reads are signed one
# object at a time and batched instead.
# --------------------------------------------------------------------------- #

# A repository-RELATIVE object suffix, and nothing else. THIS IS A KEY-SAFETY
# CONTROL: a suffix is joined onto the prefix the server derives from the job,
# so a `..` segment, a leading slash, a backslash, an empty segment or a control
# character would address an object outside the repository on any layer between
# here and the bucket that normalises a path.
#
# THE VALIDATOR IS `_safe_object_suffix` ITSELF, not a regex restating it.
# `Field(pattern=...)` would be the obvious move and is the wrong one twice
# over: pydantic-core's default regex engine has no lookahead, so the `..` and
# empty-segment rules cannot be expressed as one pattern at all; and a second
# statement of the alphabet is a second thing to drift. `dispatch.py` records
# what drift costs here -- a hand-rolled equivalent silently omitted the
# control-character check, so `manifests/x\ty` would have reached an S3 key.
#
# Applied at the wire is the FIRST of three layers, not the only one:
# `presign_backup_read` runs the same validator again before signing, and the
# object store's own key handling is the third.
def _wire_safe_suffix(value: str) -> str:
    _safe_object_suffix(value)
    return value


# THE READ BATCH BOUND, and deliberately NOT the write side's 1024. The write
# exchange moves no bytes, so its batch size is free; here every grant is ~1 MiB
# to fetch, and 1024 keys is ~1 GiB per batch -- long enough on a slow link that
# the URLs expire mid-batch and the restore thrashes. 256 keys is ~256 MiB,
# about 3.5 minutes on a 10 Mbps link inside a 15-minute window. Erring small is
# safe because re-requesting is cheap; erring large strands work.
READ_BATCH_MAX = 256

ObjectSuffix = Annotated[str, Field(max_length=512), AfterValidator(_wire_safe_suffix)]


class BackupReadQueryMsg(_Msg):
    """agent -> server: "sign me a GET for each of these objects in my
    repository."

    Carries NO repository id, NO storage prefix and NO keys -- only SUFFIXES.
    The server joins each onto the prefix it derives from the job IT signed, so
    an agent cannot express a location outside its own repository: the boundary
    is a property of the construction, not of remembering to validate.

    Compatibility: strictly additive. An older control plane rejects this kind
    at `parse_agent_message`; the agent's restore then fails on the unanswered
    query with a stated reason, which is no worse than the refusal `Get` and
    `GetRef` return today.
    """

    kind: Literal["backup_read_query"]
    job_id: str
    # Opaque agent-minted correlation token, echoed back verbatim. One restore
    # issues many batches, and the replies arrive on the same connection as
    # everything else.
    query_id: str = Field(min_length=1, max_length=64)
    suffixes: list[ObjectSuffix] = Field(min_length=1, max_length=READ_BATCH_MAX)


class BackupReadGrantItem(BaseModel):
    """One signed GET. A bearer credential for its lifetime: never logged,
    never audited, never returned by an API route."""

    model_config = ConfigDict(extra="forbid")
    suffix: str
    url: str


class BackupReadGrantMsg(_Msg):
    """server -> agent: one short-lived signed GET per requested suffix, in the
    order asked.

    There is deliberately NO error variant, for the reason
    `BackupChunkGrantMsg` states: a reply that distinguished "no such job" from
    "not your job" would be an oracle a compromised endpoint could walk. Every
    authorization refusal is silence plus an audit record. A suffix or batch
    size the agent should never have sent is refused a layer earlier still, by
    `BackupReadQueryMsg`'s own validation, so it reaches no handler.
    """

    kind: Literal["backup_read_grant"]
    job_id: str
    query_id: str
    grants: list[BackupReadGrantItem] = Field(default_factory=list, max_length=READ_BATCH_MAX)
    expires_at: str  # RFC3339; the grant's, which is the job's or sooner
```

Add to the imports at the top of the file:

```python
from pydantic import AfterValidator

from app.core.storage import _safe_object_suffix
```

`app.core.storage` imports only `app.core.config`, so this creates no cycle;
`app/modules/backup/dispatch.py:27` already imports the same validator across
module boundaries, so this follows an existing path rather than opening a new
one.

Then add `BackupReadQueryMsg` to the `AgentMessage` union:

```python
AgentMessage = Annotated[
    JobStatusMsg
    | JobLogMsg
    | JobResultMsg
    | HeartbeatMsg
    | SecurityEventsMsg
    | HealthReportMsg
    | BackupChunkQueryMsg
    | BackupReadQueryMsg,
    Field(discriminator="kind"),
]
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && pytest tests/test_gateway_protocol.py -q && ruff check . && mypy app`
Expected: PASS — 11 read tests (9 parametrized + 2); ruff and mypy clean.

Every case in the parametrized list is one `_safe_object_suffix` already
rejects, so a failure there means the validator is not being reached — check the
`Annotated` wiring, not the alphabet.

- [ ] **Step 5: Commit**

```bash
git add backend/app/gateway/protocol.py backend/tests/test_gateway_protocol.py
git commit -m "feat(gateway): the backup read query and grant message kinds"
```

---

### Task 4: `presign_backup_read` — the batch signing half

**Files:**
- Modify: `backend/app/core/storage.py` (beside `presign_backup_object`, line ~374)
- Test: `backend/tests/test_storage_presign_read.py` (create)

**Interfaces:**
- Produces: `ObjectStore.presign_backup_read(self, org_id: UUID, repository_id: UUID, suffixes: list[str], *, expires_in: int) -> list[dict[str, str]]`, returning `[{"suffix": ..., "url": ...}]` in the order given, and raising `ValueError` if ANY suffix is unsafe — before signing anything.

**Read first:** `presign_backup_object` in the same file. It already signs one
GET under one repository prefix and already validates with
`_safe_object_suffix`. This method is a batch wrapper over it and must not
reimplement either half — an earlier hand-rolled validator in
`dispatch.py::_resolve_manifest_ref` was weaker than the shared one (it missed
the control-character check), which is the exact drift this reuse prevents.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_storage_presign_read.py`:

```python
from urllib.parse import unquote, urlparse
from uuid import uuid4

import pytest

from app.core.storage import ObjectStore


@pytest.mark.parametrize(
    "bad",
    ["../../etc/passwd", "chunks/../../../secrets", "/absolute/key", "chunks\\ab\\abcd", ""],
)
def test_an_unsafe_suffix_refuses_the_whole_batch(object_store: ObjectStore, bad: str) -> None:
    """One bad suffix must refuse everything, not sign the rest.

    Signing the good ones would hand an agent that is probing a partial success
    to learn from, and would put the grant before the refusal.
    """
    with pytest.raises(ValueError):
        object_store.presign_backup_read(
            uuid4(), uuid4(), ["chunks/ab/abcd", bad], expires_in=900
        )


def test_every_signed_url_addresses_a_key_under_the_repository_prefix(
    object_store: ObjectStore,
) -> None:
    org, repo = uuid4(), uuid4()
    got = object_store.presign_backup_read(
        org, repo, ["chunks/ab/abcd", "manifests/m1"], expires_in=900
    )
    assert [g["suffix"] for g in got] == ["chunks/ab/abcd", "manifests/m1"]

    prefix = object_store.backup_prefix(org, repo)
    for g in got:
        key = unquote(urlparse(g["url"]).path).lstrip("/")
        # The bucket may be in the path (path-style) or in the host
        # (virtual-host style); either way the key must start under the prefix.
        assert f"{prefix}/" in key, f"a signed URL addressed a key outside the repository prefix"


def test_an_out_of_range_lifetime_is_refused(object_store: ObjectStore) -> None:
    # `_check_ttl`'s bound, reached through this method rather than assumed.
    with pytest.raises(ValueError):
        object_store.presign_backup_read(uuid4(), uuid4(), ["chunks/ab/abcd"], expires_in=0)


def test_an_empty_batch_is_refused(object_store: ObjectStore) -> None:
    with pytest.raises(ValueError):
        object_store.presign_backup_read(uuid4(), uuid4(), [], expires_in=900)
```

If `tests/` has no `object_store` fixture, add one to this file that builds an
`ObjectStore` the same way the existing grant tests do — check
`backend/tests/test_storage_grants.py` and reuse its fixture rather than
inventing a second construction path.

- [ ] **Step 2: Run and watch it fail**

Run: `cd backend && pytest tests/test_storage_presign_read.py -q`
Expected: FAIL — `AttributeError: 'ObjectStore' object has no attribute 'presign_backup_read'`.

- [ ] **Step 3: Implement**

Immediately after `presign_backup_object`:

```python
    def presign_backup_read(
        self,
        org_id: UUID,
        repository_id: UUID,
        suffixes: list[str],
        *,
        expires_in: int,
    ) -> list[dict[str, str]]:
        """Short-lived GET grants for a batch of objects in one repository.

        The read counterpart of `presign_backup_write`, and deliberately NOT
        prefix-scoped: SigV4 query signing cannot express a prefix READ, which
        is why each object is signed individually and the agent asks in batches
        (`docs/modules/backup/spec.md` §6.1 item 1).

        The caller passes SUFFIXES, never keys. Each is joined onto the prefix
        DERIVED from `org_id`/`repository_id` -- ids this process is already
        authorized for -- so a caller cannot express a location outside the
        repository.

        VALIDATION HAPPENS BEFORE ANY SIGNING, not per item as it goes: one
        unsafe suffix must refuse the WHOLE batch, and a loop that signed as it
        validated would have already minted grants for everything ahead of the
        bad one. Signing part of a batch hands an agent that is probing a
        partial success to learn from.

        `_safe_object_suffix` is the validator, shared with
        `presign_backup_object` rather than restated here: an earlier
        hand-rolled equivalent elsewhere in this codebase silently omitted its
        control-character check.
        """
        if not suffixes:
            raise ValueError("a read grant batch must name at least one object")
        _check_ttl(expires_in)
        # Validate everything first; `_safe_object_suffix` raises on the first
        # bad one and nothing has been signed yet.
        safe = [_safe_object_suffix(suffix) for suffix in suffixes]
        return [
            {
                "suffix": suffix,
                "url": self._presign(
                    "get_object",
                    self._backup_key(org_id, repository_id, safe_suffix),
                    expires_in=expires_in,
                ),
            }
            for suffix, safe_suffix in zip(suffixes, safe, strict=True)
        ]
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && pytest tests/test_storage_presign_read.py -q && ruff check . && mypy app`
Expected: PASS, 8 tests (5 parametrized + 3); ruff and mypy clean.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/storage.py backend/tests/test_storage_presign_read.py
git commit -m "feat(backup): presign_backup_read, refusing a batch with any unsafe suffix"
```

---

### Task 5: `answer_read_query`, and the gateway route

**Files:**
- Create: `backend/app/modules/backup/read.py`
- Modify: `backend/app/gateway/dispatch.py` (beside the `BackupChunkQueryMsg` branch, line ~382)
- Test: `backend/tests/test_backup_read_query.py` (create)

**Interfaces:**
- Consumes: `ObjectStore.presign_backup_read` (Task 4), `BackupReadQueryMsg` / `BackupReadGrantMsg` (Task 3).
- Produces:
  `READ_GRANT_TTL = 900`;
  `async def answer_read_query(session: AsyncSession, object_store: ObjectStore, msg: BackupReadQueryMsg, *, org_id: UUID, device_id: UUID, now: datetime | None = None) -> BackupReadGrantMsg | None`;
  `async def handle_read_query(app_engine: AsyncEngine, redis: "aioredis.Redis", msg: BackupReadQueryMsg, *, device_id: UUID, org_id: UUID, object_store: ObjectStore | None = None) -> BackupReadGrantMsg | None`.
  `None` means refused, audited, and nothing sent.

**Read first:** `backend/app/modules/backup/dedup.py` — `_resolve` (line 147),
`_audit_refusal` (line 125), `answer_chunk_query` (line 283) and
`handle_chunk_query` (line 433). This module is their read-side twin: same
signatures, same silent-refusal discipline, same publish-after-commit rule.

**The resolver cannot be reused, and this is the crux of the task.**
`dedup.py::_resolve` requires `job.type == "backup.run"` and a `BackupRun` row
in state `running`. A read query comes from a `backup.restore` job, which has a
`RestoreJob` row instead. So this module needs its own `_resolve_restore` with
the identical shape and the identical refusal-reason vocabulary.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_backup_read_query.py`. Build its fixtures the way
`backend/tests/test_backup_dedup.py` builds a job + run + repository — read that
file and mirror it, substituting a `backup.restore` job with a `RestoreJob` row
in state `running` and a `BackupPoint` whose `repository_id` is the repository.

```python
async def test_a_live_restore_job_gets_one_grant_per_suffix(session, store, device, restore_job):
    reply = await answer_read_query(
        session,
        store,
        _query(restore_job, ["chunks/ab/abcd", "manifests/m1"]),
        org_id=device.org_id,
        device_id=device.id,
    )
    assert reply is not None
    assert reply.kind == "backup_read_grant"
    assert reply.query_id == "q1"
    assert [g.suffix for g in reply.grants] == ["chunks/ab/abcd", "manifests/m1"]


async def test_the_repository_comes_from_the_job_never_the_payload(
    session, store, device, restore_job, other_repository
):
    """An agent naming its own repository would be naming its own scope.

    The message model carries no repository id at all, so this asserts the
    property the model enforces: the signed keys land under the repository the
    JOB resolves to.
    """
    reply = await answer_read_query(
        session, store, _query(restore_job, ["chunks/ab/abcd"]),
        org_id=device.org_id, device_id=device.id,
    )
    assert reply is not None
    prefix = store.backup_prefix(device.org_id, restore_job.repository_id)
    assert f"{prefix}/" in unquote(urlparse(reply.grants[0].url).path)
    other = store.backup_prefix(device.org_id, other_repository.id)
    assert other not in unquote(urlparse(reply.grants[0].url).path)


async def test_a_job_belonging_to_another_device_is_refused_in_silence(
    session, store, device, other_device_restore_job
):
    reply = await answer_read_query(
        session, store, _query(other_device_restore_job, ["chunks/ab/abcd"]),
        org_id=device.org_id, device_id=device.id,
    )
    assert reply is None, "an authorization refusal must be silence, never a distinguishable reply"


async def test_a_backup_run_job_cannot_borrow_authorization_for_a_read(
    session, store, device, run_job
):
    """A read grant must come from the job type that READS a repository."""
    reply = await answer_read_query(
        session, store, _query(run_job, ["chunks/ab/abcd"]),
        org_id=device.org_id, device_id=device.id,
    )
    assert reply is None


async def test_a_finished_restore_cannot_still_mint_grants(session, store, device, restore_job):
    restore_job_row = await _restore_row(session, restore_job)
    restore_job_row.state = "success"
    await session.flush()
    reply = await answer_read_query(
        session, store, _query(restore_job, ["chunks/ab/abcd"]),
        org_id=device.org_id, device_id=device.id,
    )
    assert reply is None


async def test_a_storage_ref_that_does_not_match_the_derived_prefix_yields_no_grant(
    session, store, device, restore_job, repository
):
    """`answer_chunk_query`'s rule, applied here: a `storage_ref` column somehow
    made to name another tenant's prefix yields NO grant, not a signed one over
    whatever it names."""
    repository.storage_ref = "some-other-org/backup/whatever"
    await session.flush()
    reply = await answer_read_query(
        session, store, _query(restore_job, ["chunks/ab/abcd"]),
        org_id=device.org_id, device_id=device.id,
    )
    assert reply is None


async def test_the_grant_never_outlives_the_job(session, store, device, restore_job):
    """The grant dies with the job that authorized it, and never later."""
    restore_job.expires_at = datetime.now(UTC) + timedelta(seconds=30)
    await session.flush()
    reply = await answer_read_query(
        session, store, _query(restore_job, ["chunks/ab/abcd"]),
        org_id=device.org_id, device_id=device.id,
    )
    assert reply is not None
    assert datetime.fromisoformat(reply.expires_at) <= restore_job.expires_at


def _query(job, suffixes: list[str]) -> BackupReadQueryMsg:
    return BackupReadQueryMsg(
        v=1, kind="backup_read_query", job_id=str(job.id), query_id="q1", suffixes=suffixes
    )
```

- [ ] **Step 2: Run and watch it fail**

Run: `cd backend && pytest tests/test_backup_read_query.py -q`
Expected: FAIL — `ImportError: cannot import name 'answer_read_query'`.

- [ ] **Step 3: Implement `read.py`**

```python
"""The READ half of the repository exchange (`backup_read_query` ->
`backup_read_grant`).

`dedup.py` authorizes WRITES for a `backup.run`; this module authorizes READS
for a `backup.restore`. The two are deliberately separate modules over the same
discipline rather than one parameterised path: they resolve different job types
against different run tables, and a shared resolver would put the capability
that TAKES backups on the same code as the one that returns them.

THE QUERY CARRIES SUFFIXES, NEVER KEYS. Every key is derived here, by joining a
suffix onto the prefix computed from ids this process is already authorized for.
An agent cannot express a location outside its own repository, so the boundary
is a property of the construction rather than of remembering to validate. A
suffix whose shape could escape anyway is refused a layer earlier, by
`BackupReadQueryMsg`, and a third time by `_safe_object_suffix` inside
`presign_backup_read`.

EVERY REFUSAL IS SILENCE PLUS AN AUDIT RECORD. `BackupChunkGrantMsg`'s reasoning
applies unchanged: a reply that said "no such job" and a reply that said "not
your job" would together be an oracle a compromised endpoint could walk. The
agent sees a refusal as `readreply`'s bounded timeout.
"""
```

Then the body. Take the imports (`audit_service`, `org_session`, `Job`,
`BackupRepository`, `_LIVE_JOB_STATUSES`) from `dedup.py`'s own import block —
copy it and drop what this module does not use.

```python
# THE READ WINDOW. 256 suffixes is ~256 MiB at the pinned 1 MiB average chunk
# size -- about 3.5 minutes on a 10 Mbps link, comfortably inside this window,
# so a batch does not expire mid-fetch. Erring short is safe because a read is
# idempotent and a re-request is cheap; erring long widens what a leaked URL is
# worth. Deliberately NOT tied to the job's `expires_at`: a job with a generous
# lifetime would silently widen the signing window, which is the property being
# controlled. The job's lifetime is still a CEILING, applied below.
READ_GRANT_TTL = 900


@dataclass(frozen=True)
class _ResolvedRestore:
    """Everything the answer needs, all of it read off server-side rows."""

    job: Job
    repository_id: UUID
    storage_ref: str


async def _audit_refusal(
    session: AsyncSession, *, org_id: UUID, device_id: UUID, job_id: str, reason: str
) -> None:
    """The twin of `dedup.py::_audit_refusal`, and it must stay in step with it.

    `reason` is a word from the fixed vocabulary below -- never a suffix, never
    a key, never an exception message. This row is hash-chained and permanent.
    """
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="agent",
        actor_id=None,
        action="backup.read_refused",
        target_type="device",
        target_id=str(device_id),
        # `job_id` is agent-supplied and may be anything the wire model allowed;
        # bounded here so a hostile value cannot bloat an audit row.
        metadata={"reason": reason, "job_id": job_id[:64]},
    )


async def _resolve_restore(
    session: AsyncSession, *, org_id: UUID, device_id: UUID, job_id: str, now: datetime
) -> tuple[_ResolvedRestore | None, str]:
    """(what the job authorizes, or None and a reason code).

    `dedup.py::_resolve`'s structure and reason vocabulary, over the RESTORE
    side's rows: a `backup.restore` job with a live `RestoreJob`, whose
    repository is reached through the backup point it is restoring.

    EVERY lookup carries `org_id` explicitly (invariant #5) and the device
    binding is checked against the certificate-resolved `device_id`, never
    against anything in the message.
    """
    try:
        parsed = UUID(job_id)
    except ValueError:
        return None, "malformed_job_id"

    job = (
        await session.execute(select(Job).where(Job.org_id == org_id, Job.id == parsed))
    ).scalar_one_or_none()
    if job is None:
        return None, "no_such_job"
    if job.type != "backup.restore":
        # A read grant over a repository must come from the job type that READS
        # one. A `backup.run` reaching here is either a bug or an endpoint
        # trying to borrow authorization from a job it does hold.
        return None, "wrong_job_type"
    if job.target_type != "device" or job.target_id != device_id:
        return None, "wrong_device"
    if job.status not in _LIVE_JOB_STATUSES:
        return None, "job_not_live"
    if job.expires_at <= now:
        return None, "job_expired"

    restore = (
        await session.execute(
            select(RestoreJob).where(RestoreJob.org_id == org_id, RestoreJob.job_id == job.id)
        )
    ).scalar_one_or_none()
    if restore is None:
        return None, "no_such_restore"
    if restore.state != "running":
        # A finished restore must not still mint grants: the window closes when
        # the work does, not when the job's `expires_at` happens to pass.
        return None, "restore_not_running"

    point = (
        await session.execute(
            select(BackupPoint).where(
                BackupPoint.org_id == org_id, BackupPoint.id == restore.backup_point_id
            )
        )
    ).scalar_one_or_none()
    if point is None:
        return None, "no_such_point"

    repo = (
        await session.execute(
            select(BackupRepository).where(
                BackupRepository.org_id == org_id, BackupRepository.id == point.repository_id
            )
        )
    ).scalar_one_or_none()
    if repo is None:
        return None, "no_such_repository"
    if repo.state != "active":
        return None, "repository_not_active"

    return _ResolvedRestore(job=job, repository_id=repo.id, storage_ref=repo.storage_ref), ""


async def answer_read_query(
    session: AsyncSession,
    object_store: ObjectStore,
    msg: BackupReadQueryMsg,
    *,
    org_id: UUID,
    device_id: UUID,
    now: datetime | None = None,
) -> BackupReadGrantMsg | None:
    """Answer one read query, or refuse it silently.

    `org_id`/`device_id` come from the gateway's mTLS binding. Returns the reply
    the caller should send to this device, or `None` -- in which case an audit
    record has been written and nothing must be sent.
    """
    now = now or datetime.now(UTC)

    resolved, reason = await _resolve_restore(
        session, org_id=org_id, device_id=device_id, job_id=msg.job_id, now=now
    )
    if resolved is None:
        await _audit_refusal(
            session, org_id=org_id, device_id=device_id, job_id=msg.job_id, reason=reason
        )
        return None

    # THE PREFIX IS DERIVED AND THE STORED VALUE IS ONLY CHECKED AGAINST IT --
    # `answer_chunk_query`'s rule, unchanged. A `storage_ref` column somehow
    # made to name another tenant's prefix yields NO grant at all, rather than a
    # signed one over whatever it names.
    derived_prefix = object_store.backup_prefix(org_id, resolved.repository_id)
    if resolved.storage_ref != derived_prefix:
        await _audit_refusal(
            session,
            org_id=org_id,
            device_id=device_id,
            job_id=msg.job_id,
            reason="storage_ref_mismatch",
        )
        return None

    # The grant dies with the job that authorized it, and never later. The job's
    # remaining life is a CEILING on the window, not the window itself.
    expires_in = min(READ_GRANT_TTL, int((resolved.job.expires_at - now).total_seconds()))
    if expires_in < 1:
        await _audit_refusal(
            session, org_id=org_id, device_id=device_id, job_id=msg.job_id, reason="job_expired"
        )
        return None

    try:
        signed = object_store.presign_backup_read(
            org_id, resolved.repository_id, list(msg.suffixes), expires_in=expires_in
        )
    except ValueError:
        # A suffix got past `BackupReadQueryMsg`'s pattern. That is evidence of
        # a compromised manifest or a drifted validator, not a formatting
        # quirk, and the whole batch is refused. THE EXCEPTION MESSAGE IS NOT
        # RECORDED: it echoes the suffix, and this row is permanent.
        await _audit_refusal(
            session, org_id=org_id, device_id=device_id, job_id=msg.job_id, reason="unsafe_suffix"
        )
        return None

    return BackupReadGrantMsg(
        v=1,
        kind="backup_read_grant",
        job_id=str(resolved.job.id),
        query_id=msg.query_id,
        grants=[
            BackupReadGrantItem(suffix=str(g["suffix"]), url=str(g["url"])) for g in signed
        ],
        expires_at=(now + timedelta(seconds=expires_in)).replace(microsecond=0).isoformat(),
    )


async def handle_read_query(
    app_engine: AsyncEngine,
    redis: "aioredis.Redis",
    msg: BackupReadQueryMsg,
    *,
    device_id: UUID,
    org_id: UUID,
    object_store: ObjectStore | None = None,
) -> BackupReadGrantMsg | None:
    """Answer one query and send the reply back down the device's channel.

    THE REPLY IS PUBLISHED ONLY AFTER THE TENANT TRANSACTION COMMITS, matching
    `handle_chunk_query`. Nothing here writes application rows, but the audit
    record for a refusal does, and a reply that raced its own audit row would
    put a grant on the wire that the log did not yet know about.

    It travels on `dispatch:{device_id}` -- the channel that already carries
    signed job envelopes to whichever gateway replica holds this device's
    connection. It is NOT a signed job and does not pretend to be one: it
    commands nothing, and invariant #1 governs what the agent EXECUTES.

    Returns the reply for a caller that wants to assert on it; `None` means
    refused, audited, and nothing sent.
    """
    import json

    from app.gateway.dispatch import dispatch_channel

    store = object_store or gateway_object_store()
    async with org_session(org_id, engine=app_engine) as session:
        reply = await answer_read_query(
            session, store, msg, org_id=org_id, device_id=device_id
        )
    if reply is None:
        return None
    await redis.publish(
        dispatch_channel(device_id), json.dumps(reply.model_dump(mode="json"))
    )
    return reply
```

`gateway_object_store` is `dedup.py`'s lazily-constructed module-level store.
Import it from there rather than adding a second one — constructing an
`ObjectStore` reaches the network, and two of them per gateway process is two
`head_bucket` round trips where one will do.

- [ ] **Step 4: Route the kind in the gateway**

In `backend/app/gateway/dispatch.py`, add `BackupReadQueryMsg` to the imports at
line ~30 and this branch immediately after the `BackupChunkQueryMsg` branch:

```python
    if isinstance(msg, BackupReadQueryMsg):
        # The backup READ exchange. Like the dedup query above it carries a
        # `job_id` and is NOT a job-state message: it does not touch the job
        # row, and it must not fall through to `_load_job_for_device` below,
        # whose only application-level predicate is `Job.id` -- this exchange's
        # own resolver carries `Job.org_id` too, and needs the refusal to be
        # silent rather than an identity-mismatch audit.
        from app.modules.backup.read import handle_read_query

        await handle_read_query(
            app_engine, redis, msg, device_id=device_id, org_id=org_id
        )
        return
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && pytest tests/test_backup_read_query.py tests/test_backup_dedup.py -q && ruff check . && mypy app`
Expected: PASS — 7 new tests, the dedup suite still green, ruff and mypy clean.

- [ ] **Step 6: Commit**

```bash
git add backend/app/modules/backup/read.py backend/app/gateway/dispatch.py \
        backend/tests/test_backup_read_query.py
git commit -m "feat(backup): answer a read query, scoped to the job's own repository"
```

---

### Task 6: `backupstore.Get` and `GetRef`

**Files:**
- Modify: `agent/internal/backupstore/backupstore.go`
- Test: `agent/internal/backupstore/backupstore_read_test.go` (create)

**Interfaces:**
- Consumes: `wire.ReadQuery` / `wire.ReadGrant` (Task 1); `backup.Repository.JobID` (Task 7 adds the field — **add it as part of this task's first commit if Task 7 has not run yet**, since this task cannot compile without it).
- Produces:
  `type ReadGranter interface { Query(ctx context.Context, q wire.ReadQuery) (wire.ReadGrant, error) }`;
  `func Opener(g ReadGranter) func(context.Context, backup.Repository) (backup.ObjectStore, error)`;
  `func Open(ctx context.Context, repo backup.Repository) (backup.ObjectStore, error)` kept as `Opener(nil)`;
  `(*store).Expect(suffixes []string)` satisfying `backup.BatchReader`;
  working `Get(ctx, suffix)` and `GetRef(ctx, ref)`.

**The batching lives here, not in restore.** `restore.go` streams one chunk at a
time into one file at a time, and rewriting that into a batch loop would put the
manifest walk and the transport window in the same code. Instead the restorer
announces the fetch ORDER through `Expect`, and this store asks for grants in
windows of 256 starting at whatever `Get` is currently asking for. A store that
was never given an order still works — its window is 1 — which is correct and
slow, and is what every existing test fake gets for free.

- [ ] **Step 1: Write the failing tests**

Create `agent/internal/backupstore/backupstore_read_test.go`. Model
`newTestStore` on how `backupstore_test.go` already builds a store against
`httptest.NewTLSServer` (it sets `testTLSConfig`); reuse that seam rather than
adding a second one.

```go
type fakeGranter struct {
	mu       sync.Mutex
	queries  int
	batches  [][]string
	url      string // every suffix resolves to this URL
	err      error
}

func (f *fakeGranter) Query(_ context.Context, q wire.ReadQuery) (wire.ReadGrant, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.queries++
	f.batches = append(f.batches, append([]string(nil), q.Suffixes...))
	if f.err != nil {
		return wire.ReadGrant{}, f.err
	}
	g := wire.ReadGrant{JobID: q.JobID, QueryID: q.QueryID}
	for _, s := range q.Suffixes {
		g.Grants = append(g.Grants, wire.ReadGrantItem{Suffix: s, URL: f.url})
	}
	return g, nil
}

func TestGetFetchesTheBytesTheGrantPointsAt(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("chunk-bytes"))
	}))
	defer srv.Close()
	g := &fakeGranter{url: srv.URL}
	s := newTestStore(t, g)

	got, err := s.Get(context.Background(), "chunks/ab/abcd")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if string(got) != "chunk-bytes" {
		t.Fatalf("Get = %q, want %q", got, "chunk-bytes")
	}
}

func TestExpectMakesGetAskForAWholeWindowAtOnce(t *testing.T) {
	// 600 chunks must cost 3 batches, not 600 round trips. One presigned URL
	// per chunk in job params is what spec §6.1 rejected outright.
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("x"))
	}))
	defer srv.Close()
	g := &fakeGranter{url: srv.URL}
	s := newTestStore(t, g)

	want := make([]string, 600)
	for i := range want {
		want[i] = fmt.Sprintf("chunks/ab/%04x", i)
	}
	s.Expect(want)
	for _, suffix := range want {
		if _, err := s.Get(context.Background(), suffix); err != nil {
			t.Fatalf("Get(%s): %v", suffix, err)
		}
	}

	if g.queries != 3 {
		t.Fatalf("fetching %d suffixes took %d batches, want 3 (ceil(600/256))", len(want), g.queries)
	}
	for i, b := range g.batches {
		if len(b) > readBatchMax {
			t.Fatalf("batch %d asked for %d suffixes, want <= %d", i, len(b), readBatchMax)
		}
	}
	if n := len(g.batches[0]); n != readBatchMax {
		t.Fatalf("the first batch asked for %d suffixes, want a full window of %d", n, readBatchMax)
	}
}

func TestAWindowThatLandsExactlyOnTheBoundaryTakesOneBatch(t *testing.T) {
	// The off-by-one the spec's §6 calls out by name.
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("x"))
	}))
	defer srv.Close()
	g := &fakeGranter{url: srv.URL}
	s := newTestStore(t, g)

	want := make([]string, readBatchMax)
	for i := range want {
		want[i] = fmt.Sprintf("chunks/ab/%04x", i)
	}
	s.Expect(want)
	for _, suffix := range want {
		if _, err := s.Get(context.Background(), suffix); err != nil {
			t.Fatalf("Get(%s): %v", suffix, err)
		}
	}
	if g.queries != 1 {
		t.Fatalf("exactly %d suffixes took %d batches, want 1", readBatchMax, g.queries)
	}
}

func TestAnExpiredUrlIsRetriedWithAFreshGrant(t *testing.T) {
	// Reads are idempotent, so an expiry costs a re-request rather than a
	// failed restore. 403 is what an expired presigned GET returns.
	var hits int32
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if atomic.AddInt32(&hits, 1) == 1 {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		_, _ = w.Write([]byte("chunk-bytes"))
	}))
	defer srv.Close()
	g := &fakeGranter{url: srv.URL}
	s := newTestStore(t, g)

	got, err := s.Get(context.Background(), "chunks/ab/abcd")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if string(got) != "chunk-bytes" {
		t.Fatalf("Get = %q after retry, want the bytes", got)
	}
	if g.queries < 2 {
		t.Fatalf("the store asked for %d grants; an expired URL must be re-requested", g.queries)
	}
}

func TestAPermanentlyForbiddenFetchGivesUpBounded(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer srv.Close()
	g := &fakeGranter{url: srv.URL}
	s := newTestStore(t, g)

	if _, err := s.Get(context.Background(), "chunks/ab/abcd"); err == nil {
		t.Fatal("a permanently forbidden fetch returned nil error; a restore would loop")
	}
	if g.queries > readAttempts {
		t.Fatalf("asked for %d grants; retry must be bounded at %d", g.queries, readAttempts)
	}
}

func TestADroppedCorrelationFailsWithTheGrantersReason(t *testing.T) {
	g := &fakeGranter{err: errors.New("readreply: gateway connection lost while awaiting a grant")}
	s := newTestStore(t, g)
	_, err := s.Get(context.Background(), "chunks/ab/abcd")
	if err == nil || !strings.Contains(err.Error(), "connection lost") {
		t.Fatalf("Get with a failing granter = %v, want it to carry the granter's reason", err)
	}
}

func TestAGrantForADifferentSuffixIsNotUsed(t *testing.T) {
	// A grant that answered with the wrong object would restore one file's
	// bytes into another's. The id check in restore.go would catch it, but
	// this store must not present it as the requested object in the first
	// place.
	g := &wrongSuffixGranter{}
	s := newTestStore(t, g)
	if _, err := s.Get(context.Background(), "chunks/ab/abcd"); err == nil {
		t.Fatal("a grant naming a different suffix was accepted")
	}
}

func TestAPlaintextGrantUrlIsRefused(t *testing.T) {
	// The write path's rule, unchanged: a grant is a bearer credential and the
	// URL carries it in the clear.
	g := &fakeGranter{url: "http://store.example/o?sig=x"}
	s := newTestStore(t, g)
	if _, err := s.Get(context.Background(), "chunks/ab/abcd"); err == nil ||
		!strings.Contains(err.Error(), "https") {
		t.Fatalf("Get against an http grant = %v, want it refused for its scheme", err)
	}
}

func TestGetRefTurnsAServerIssuedReferenceIntoAOneSuffixQuery(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("manifest-bytes"))
	}))
	defer srv.Close()
	g := &fakeGranter{url: srv.URL}
	s := newTestStore(t, g) // built with StorageRef "org1/backup/repo1"

	got, err := s.GetRef(context.Background(), "s3://the-bucket/org1/backup/repo1/manifests/m1")
	if err != nil {
		t.Fatalf("GetRef: %v", err)
	}
	if string(got) != "manifest-bytes" {
		t.Fatalf("GetRef = %q, want the manifest bytes", got)
	}
	if len(g.batches) != 1 || len(g.batches[0]) != 1 || g.batches[0][0] != "manifests/m1" {
		t.Fatalf("GetRef asked for %v, want one query for the suffix manifests/m1", g.batches)
	}
}

func TestGetRefRefusesAReferenceOutsideThisRepository(t *testing.T) {
	g := &fakeGranter{url: "https://x"}
	s := newTestStore(t, g)
	_, err := s.GetRef(context.Background(), "s3://the-bucket/other-org/backup/repo9/manifests/m1")
	if err == nil {
		t.Fatal("a manifest reference outside this repository's prefix was accepted")
	}
	if strings.Contains(err.Error(), "other-org") {
		t.Fatalf("the refusal echoed the reference: %v", err)
	}
}

func TestAStoreWithNoGranterStillRefusesWithAStatedReason(t *testing.T) {
	// `Open` (no granter) is what every existing caller and test fake gets.
	// It must refuse as a standing incapacity, so restore.go's jobs.Refusal
	// branch still reports `refused` rather than a failed restore.
	s := newTestStore(t, nil)
	_, err := s.Get(context.Background(), "chunks/ab/abcd")
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("Get with no granter = %v, want a jobs.Refusal", err)
	}
}
```

Write `wrongSuffixGranter` (returns a grant whose single item names
`"chunks/zz/other"`) in the same file.

- [ ] **Step 2: Run and watch it fail**

Run: `cd agent && go test ./internal/backupstore/ -count=1`
Expected: FAIL — `Get` still returns the standing refusal for every case.

- [ ] **Step 3: Implement**

Add above `Open`:

```go
const (
	// readBatchMax is how many suffixes one grant request covers. NOT the
	// write side's 1024: no bytes move on the write exchange, so its batch is
	// free, while every grant here is ~1 MiB to fetch. 1024 keys is ~1 GiB per
	// batch, long enough on a slow link that URLs expire mid-batch and the
	// restore thrashes. 256 (~256 MiB, ~3.5 min on 10 Mbps) sits comfortably
	// inside the server's 15-minute window. It must stay <= the control
	// plane's READ_BATCH_MAX, which rejects a larger batch at the parse layer.
	readBatchMax = 256
	// readAttempts is 1 try plus 2 retries. A read is idempotent, so an
	// expired URL costs a re-request rather than a failed restore; the bound
	// is what stops a genuinely forbidden key from looping forever.
	readAttempts = 3
	// readTimeout bounds one object fetch, end to end.
	readTimeout = 5 * time.Minute
	// maxReadObject is a backstop against a hostile or broken store answering
	// a 1 MiB chunk request with an unbounded body and exhausting this
	// agent's memory. It is NOT a tight bound: the manifest is the largest
	// object a restore reads and its size is not known here (restore params
	// carry no dedup_params), so this is deliberately generous.
	maxReadObject = 512 << 20
)

// ReadGranter mints short-lived signed GETs by asking the control plane.
//
// Deliberately narrow, and deliberately not a credential: this agent holds no
// object-store key and must never be given one (invariant #3). Every read is
// authorized by a URL the control plane signed for one job, one repository and
// one window. *readreply.Registry satisfies it.
type ReadGranter interface {
	Query(ctx context.Context, q wire.ReadQuery) (wire.ReadGrant, error)
}
```

Change `store` and the constructors:

```go
type store struct {
	client  *http.Client
	granter ReadGranter
	jobID   string
	// prefix is the repository's storage_ref, used only to turn a
	// server-issued `s3://bucket/{prefix}/{suffix}` reference back into the
	// suffix a query can carry.
	prefix string

	mu       sync.Mutex
	expected []string          // the fetch order the restorer announced
	at       int               // how far into `expected` the window has advanced
	urls     map[string]string // suffix -> signed URL, current window only
	seq      int               // mints query ids
}

// Opener returns an OpenStore function bound to a read-grant source.
// Assignable to backup.Options.OpenStore.
func Opener(g ReadGranter) func(context.Context, backup.Repository) (backup.ObjectStore, error) {
	return func(ctx context.Context, repo backup.Repository) (backup.ObjectStore, error) {
		s, err := open(ctx, repo)
		if err != nil {
			return nil, err
		}
		s.granter = g
		return s, nil
	}
}

// Open returns a job-scoped store with NO read route: Get and GetRef refuse.
// Kept for callers that only write. It dials nothing; the first byte leaves in
// Put.
func Open(ctx context.Context, repo backup.Repository) (backup.ObjectStore, error) {
	return Opener(nil)(ctx, repo)
}
```

Move today's `Open` body into `func open(_ context.Context, repo backup.Repository) (*store, error)`,
returning a `*store` that also sets `jobID: repo.JobID`, `prefix: strings.Trim(repo.StorageRef, "/")`,
`urls: map[string]string{}`, and `Timeout: readTimeout` — replacing `uploadTimeout`
would change upload behaviour, so keep `uploadTimeout` as the client timeout and
bound a read with a `context.WithTimeout` inside `fetch` instead.

Then the read half:

```go
// Expect announces the order in which suffixes are about to be fetched, so
// grants can be requested a window at a time instead of one per object.
//
// A store that is never told degrades to a window of 1: correct, and slow.
// Satisfies backup.BatchReader.
func (s *store) Expect(suffixes []string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.expected = append(s.expected[:0], suffixes...)
	s.at = 0
	s.urls = map[string]string{}
}

func (s *store) Get(ctx context.Context, suffix string) ([]byte, error) {
	if s.granter == nil {
		return nil, jobs.Refuse(noReadRoute)
	}
	if err := safeSuffix(suffix); err != nil {
		return nil, err
	}
	var last error
	for attempt := 0; attempt < readAttempts; attempt++ {
		u, err := s.grantFor(ctx, suffix)
		if err != nil {
			// A granter failure is a dropped correlation, a refused query, or
			// a lost connection. None is fixed by asking the same way again
			// inside this loop; the restore fails with the stated reason.
			return nil, fmt.Errorf("backupstore: no read grant for the requested object: %w", err)
		}
		data, retry, err := s.fetch(ctx, u)
		if err == nil {
			return data, nil
		}
		last = err
		if !retry {
			return nil, err
		}
		// The URL may simply have expired mid-window. Drop the whole window so
		// the next attempt signs a fresh one; a read is idempotent, so this
		// re-fetches identical bytes.
		s.invalidate()
		if attempt < readAttempts-1 {
			select {
			case <-time.After(retryBackoff[attempt]):
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
	}
	return nil, fmt.Errorf("backupstore: read failed after %d attempts: %w", readAttempts, last)
}

func (s *store) GetRef(ctx context.Context, ref string) ([]byte, error) {
	suffix, err := suffixOfRef(ref, s.prefix)
	if err != nil {
		return nil, err
	}
	return s.Get(ctx, suffix)
}

// suffixOfRef turns a server-issued `s3://{bucket}/{storage_ref}/{suffix}`
// reference into the repository-relative suffix a read query can carry.
//
// The manifest reaches this agent as a full reference because the control
// plane builds one (dispatch.py::_resolve_manifest_ref) -- the agent does not
// know the bucket and must not. Rather than give the manifest its own wire
// path, the suffix is recovered here and travels as an ordinary one-suffix
// query, under the same prefix rule as any chunk.
//
// The error names no part of the reference: it reaches the job result and the
// audit log.
func suffixOfRef(ref, prefix string) (string, error) {
	if prefix == "" {
		return "", errors.New("backupstore: this store has no repository prefix, so a manifest " +
			"reference cannot be resolved to a suffix")
	}
	marker := "/" + prefix + "/"
	i := strings.Index(ref, marker)
	if i < 0 {
		return "", errors.New("backupstore: the manifest reference does not lie under this " +
			"repository's prefix")
	}
	return ref[i+len(marker):], nil
}

// grantFor returns a signed URL for suffix, requesting a whole window if the
// current one does not hold it.
func (s *store) grantFor(ctx context.Context, suffix string) (string, error) {
	s.mu.Lock()
	if u, ok := s.urls[suffix]; ok {
		s.mu.Unlock()
		return u, nil
	}
	batch := s.window(suffix)
	s.seq++
	q := wire.ReadQuery{JobID: s.jobID, QueryID: fmt.Sprintf("r%d", s.seq), Suffixes: batch}
	s.mu.Unlock()

	grant, err := s.granter.Query(ctx, q)
	if err != nil {
		return "", err
	}

	s.mu.Lock()
	for _, item := range grant.Grants {
		s.urls[item.Suffix] = item.URL
	}
	u, ok := s.urls[suffix]
	s.mu.Unlock()
	if !ok {
		// A grant that answered with objects other than the one asked for
		// would, if used, restore one file's bytes as another's. restore.go
		// re-hashes every chunk and would catch it, but this store must not
		// present it as the requested object in the first place.
		return "", errors.New("backupstore: the read grant did not cover the requested object")
	}
	return u, nil
}

// window is the batch to ask for, given the suffix being fetched now. Caller
// holds s.mu.
func (s *store) window(suffix string) []string {
	// Find the requested suffix in the announced order, starting where the
	// last window ended -- a restore fetches in that order, so this is O(1) in
	// the normal case and never scans backwards over what is already done.
	for i := s.at; i < len(s.expected); i++ {
		if s.expected[i] != suffix {
			continue
		}
		end := i + readBatchMax
		if end > len(s.expected) {
			end = len(s.expected)
		}
		s.at = end
		return append([]string(nil), s.expected[i:end]...)
	}
	// Nothing announced, or a suffix outside the announced order (GetRef's
	// manifest, for one). A window of exactly what was asked for.
	return []string{suffix}
}

func (s *store) invalidate() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.urls = map[string]string{}
	// The window has to be re-requested from where it began, not from where it
	// ended, or the rest of it would never be signed again.
	s.at = 0
}

// fetch performs one GET. `retry` reports whether the failure is one a fresh
// grant could fix.
func (s *store) fetch(ctx context.Context, rawURL string) (data []byte, retry bool, err error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, false, fmt.Errorf("backupstore: unparseable read grant endpoint: %w", err)
	}
	if u.Scheme != "https" {
		// Never downgraded, not even for a lab: a signed URL is a bearer
		// credential and travels in the clear.
		return nil, false, fmt.Errorf("backupstore: read grant endpoint is %q, and only https is allowed", u.Scheme)
	}
	ctx, cancel := context.WithTimeout(ctx, readTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, false, err
	}
	resp, err := s.client.Do(req)
	if err != nil {
		if errors.Is(err, errRedirectRefused) {
			// A redirect recurs identically against the same URL, so replaying
			// it buys nothing.
			return nil, false, fmt.Errorf("%w (a redirect recurs identically, so it is not retried)", err)
		}
		return nil, true, fmt.Errorf("backupstore: fetching the object: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	switch {
	case resp.StatusCode == http.StatusOK:
	case resp.StatusCode == http.StatusForbidden, resp.StatusCode == http.StatusUnauthorized:
		// What an expired presigned GET answers. Also what a genuinely
		// unauthorized one answers -- indistinguishable from here, which is
		// why the retry is bounded rather than conditional.
		return nil, true, fmt.Errorf("backupstore: the object store answered %d "+
			"(an expired or unauthorized read grant)", resp.StatusCode)
	case resp.StatusCode >= 500:
		return nil, true, fmt.Errorf("backupstore: object store returned %d", resp.StatusCode)
	default:
		// The response body is deliberately not included: it can echo the key.
		return nil, false, fmt.Errorf("backupstore: the object store refused the read with %d", resp.StatusCode)
	}

	// LimitReader + 1 so an object AT the cap still reads and one over it is
	// detected rather than silently truncated.
	data, err = io.ReadAll(io.LimitReader(resp.Body, maxReadObject+1))
	if err != nil {
		return nil, true, fmt.Errorf("backupstore: reading the object body: %w", err)
	}
	if int64(len(data)) > maxReadObject {
		return nil, false, fmt.Errorf("backupstore: the object store returned more than the "+
			"%d-byte cap for one object", int64(maxReadObject))
	}
	return data, false, nil
}
```

Replace `readRefusal` with:

```go
// noReadRoute is what a store opened without a grant source returns. It is a
// jobs.Refusal, not an error, because it is a standing incapacity of this
// agent rather than a failed fetch -- restore.go reports the two differently.
//
// The old text here said reads were impossible ("SigV4 query signing cannot
// express a prefix-scoped READ"). They are implemented as of the
// prefix-scoped-READ cycle; see
// docs/superpowers/specs/2026-08-13-prefix-scoped-read-design.md.
const noReadRoute = "backup: this agent has no route to read from the repository. Reads " +
	"travel under short-lived GET grants the control plane signs on the read exchange, and " +
	"this store was opened without one"
```

Finally, correct the package comment's closing paragraph — it currently ends
"READS ARE NOT IMPLEMENTED" — to describe the batched read exchange, the 256/900s
window, sequential fetch, and the two known limits from spec §7.

Add `"sync"` to the imports.

- [ ] **Step 4: Run the tests**

Run: `cd agent && go test ./internal/backupstore/ -count=1 -race`
Expected: PASS, including every pre-existing upload test.

- [ ] **Step 5: Commit**

```bash
git add agent/internal/backupstore/
git commit -m "feat(agent/backupstore): read objects through batched READ grants"
```

---

### Task 7: Restore announces its fetch order

**Files:**
- Modify: `agent/internal/capabilities/backup/backup.go` (`Repository`, line ~103; `ObjectStore` docs, line ~111; the dispatch site, line ~353)
- Modify: `agent/internal/capabilities/backup/restore.go` (signature at line ~28; `openStore` call at line ~50; after `sortEntries`, line ~190)
- Modify: `agent/internal/capabilities/backup/run.go` (line ~100, to fill the new field)
- Test: `agent/internal/capabilities/backup/restore_batch_test.go` (create)

**Interfaces:**
- Consumes: `(*backupstore.store).Expect` (Task 6).
- Produces: `Repository.JobID string`; `type BatchReader interface { Expect(suffixes []string) }`; `func (s *Service) restore(ctx context.Context, jobID string, raw json.RawMessage) (*restoreResult, error)`.

- [ ] **Step 1: Write the failing test**

```go
// expectingStore records the order restore announces, and every Get.
type expectingStore struct {
	fakeStore // whatever the package's existing test double is; embed it
	expected  []string
	got       []string
}

func (e *expectingStore) Expect(suffixes []string) { e.expected = append([]string(nil), suffixes...) }

func (e *expectingStore) Get(ctx context.Context, suffix string) ([]byte, error) {
	e.got = append(e.got, suffix)
	return e.fakeStore.Get(ctx, suffix)
}

func TestRestoreAnnouncesEveryChunkItIsAboutToFetch(t *testing.T) {
	// Without the announcement the store's window is 1, which is one signed
	// URL per chunk -- the six-figure problem spec §6.1 rejected outright.
	st := newExpectingStore(t, 600) // a manifest whose selection spans 600 chunks
	runRestore(t, st)

	if len(st.expected) == 0 {
		t.Fatal("restore fetched chunks without announcing them; the store cannot batch")
	}
	if len(st.got) == 0 {
		t.Fatal("the test fetched nothing, so it proves nothing")
	}
	// Every fetched chunk must have been announced, in the order fetched.
	announced := map[string]int{}
	for i, s := range st.expected {
		announced[s] = i
	}
	last := -1
	for _, s := range st.got {
		i, ok := announced[s]
		if !ok {
			t.Fatalf("restore fetched %q, which it never announced", s)
		}
		if i < last {
			t.Fatalf("restore fetched %q out of the announced order", s)
		}
		last = i
	}
}

func TestRestorePassesItsJobIdToTheStore(t *testing.T) {
	// A read query names the job that authorizes it; an empty job id is
	// refused by the control plane in silence, which looks like a hang.
	var got Repository
	svc := newTestService(t, func(_ context.Context, repo Repository) (ObjectStore, error) {
		got = repo
		return newFakeStore(t), nil
	})
	_, _ = svc.restore(context.Background(), "job-42", testRestoreParams(t))
	if got.JobID != "job-42" {
		t.Fatalf("the store was opened with JobID %q, want job-42", got.JobID)
	}
}
```

Build `newExpectingStore` / `runRestore` / `newTestService` on the package's
existing test doubles in `helpers_test.go` and `roundtrip_test.go` — do not add
a second fake-store construction path.

- [ ] **Step 2: Run and watch it fail**

Run: `cd agent && go test ./internal/capabilities/backup/ -run 'Restore(Announces|PassesItsJobId)' -count=1`
Expected: FAIL — `restore` takes no `jobID`, and nothing calls `Expect`.

- [ ] **Step 3: Implement**

In `backup.go`, add the field to `Repository`:

```go
	// JobID is the job whose signature authorizes this store's grants. A read
	// query names it, and the control plane resolves the repository, the
	// prefix and the tenancy from the job IT signed -- never from anything the
	// agent sends.
	JobID string
```

and the optional seam beside `ObjectStore`:

```go
// BatchReader is an ObjectStore that can be told, in advance, the order in
// which objects are about to be fetched.
//
// It exists because a read grant is signed per object: without an order to
// look ahead through, a store must ask for one grant per Get, which is the
// six-figure round-trip problem the read exchange exists to remove. It is
// OPTIONAL -- a store that does not implement it still works, one object per
// grant, which is correct and slow.
type BatchReader interface {
	Expect(suffixes []string)
}
```

Update `ObjectStore`'s header comment: reads are implemented; the split it
describes ("WRITES ARE SOLVED. READS ARE NOT") is no longer true. State what is
true — writes travel under one prefix-scoped POST policy, reads under batched
per-object GET grants, and neither is a credential this agent holds.

Also update `GetRef`'s doc comment: it still takes a full server-issued
reference, and `backupstore` now resolves that to a repository-relative suffix
before querying.

In `backup.go`'s dispatch site: `res, err := s.restore(ctx, job.ID(), job.Params())`.

In `run.go` line ~100 and `restore.go` line ~50, add `JobID: jobID` to the
`Repository` literal. `run` already has `jobID`; `restore` gains the parameter.

In `restore.go`, immediately after `sortEntries(entries)`:

```go
	// Announce the fetch order so the store can ask for grants a window at a
	// time. The order is exactly the one the two passes below fetch in:
	// symlinks carry no chunks, so pass 0's order is the whole order. Distinct
	// ids only -- a chunk shared by two files is fetched once per reference,
	// and the store's window already holds its URL the second time.
	if br, ok := store.(BatchReader); ok {
		seen := make(map[string]struct{}, len(entries))
		order := make([]string, 0, len(entries))
		for i := range entries {
			e := entries[i]
			if e.Kind == kindSymlink {
				continue
			}
			if p.Mode == modeRestore && !selects(selection, e.Path) {
				continue
			}
			for _, id := range e.ChunkIDs {
				if !validChunkID(id) {
					continue
				}
				suffix := chunkSuffix(id)
				if _, dup := seen[suffix]; dup {
					continue
				}
				seen[suffix] = struct{}{}
				order = append(order, suffix)
			}
		}
		br.Expect(order)
	}
```

- [ ] **Step 4: Run the tests**

Run: `cd agent && go test ./internal/capabilities/backup/ -count=1 -race`
Expected: PASS, including the whole existing backup suite.

- [ ] **Step 5: Commit**

```bash
git add agent/internal/capabilities/backup/
git commit -m "feat(agent/backup): restore announces its fetch order so reads batch"
```

---

### Task 8: Wire it into the running agent

**Files:**
- Modify: `agent/cmd/agent/main.go` (beside the `chunkreply` wiring, ~line 277)
- Test: `agent/internal/capabilities/integration_test.go` (extend)

- [ ] **Step 1: Route the grant and bind the granter**

Construct a `readreply.New(log)` beside the existing `chunkreply` registry.
Attach and detach it on the SAME connection events — a read registry that missed
a `Detach` would leave a restore's fetch waiting out its full deadline after a
reconnect. Route `*wire.ReadGrant` from `Decode` to `readRegistry.Deliver`, and
change `OpenStore: backupstore.Open` to `OpenStore: backupstore.Opener(readRegistry)`.

Everything that is not a `*wire.ChunkGrant` or a `*wire.ReadGrant` still goes to
the runner, unchanged.

- [ ] **Step 2: Extend the integration test**

Assert that a `backup_read_grant` frame arriving from the fake gateway reaches
the read registry and NOT the job runner — a grant misrouted to the runner would
be refused as an unknown job type, which is a confusing way to find a routing
bug. Model it on whichever existing case asserts the same thing for
`backup_chunk_grant`.

- [ ] **Step 3: Run**

Run: `cd agent && gofmt -l . && go vet ./... && GOOS=windows go vet ./... && GOOS=darwin go vet ./... && go test ./... -count=1 -race`
Expected: all pass, `gofmt -l` prints nothing.

- [ ] **Step 4: Commit**

```bash
git add agent/cmd/agent/main.go agent/internal/capabilities/integration_test.go
git commit -m "feat(agent): route read grants and bind the store's grant source"
```

---

### Task 9: The live measurement grows a restore leg

**Files:**
- Modify: `scripts/verify_backup_run.py`

**Why this task is not optional:** `docs/modules/backup/spec.md` §6.1 records
that `moto` accepts a policy-violating key and answers `204`, so every unit test
of a grant asserts the signed document and nothing else. A restore that passes
only against a fake proves the same nothing.

- [ ] **Step 1: Add the leg**

After the existing write measurement, against the same real MinIO behind real
Traefik — reusing the run's own repository, device and job plumbing rather than
building a second harness:

1. dispatch a `backup.restore` for the point the write leg just produced,
   into a scratch directory;
2. `check()` that the restored tree matches the seeded tree byte for byte,
   including the file count and total bytes `_seed_tree` returned;
3. `check()` that a signed GET for a key OUTSIDE the repository prefix is
   answered **403** by MinIO — sign one with
   `presign_backup_read`-style parameters against a key under a different
   `{org}/backup/{repo}/` and confirm the store rejects it. This is the
   assertion no unit test in this repository can make;
4. `check()` that a compliant signed GET is answered **200**;
5. `drove()` that the read exchange actually ran — count the
   `backup_read_query` frames observed on the dispatch channel via the existing
   `_drain_grants` mechanism, and assert it is `ceil(chunks/256)` rather than
   one per chunk.

- [ ] **Step 2: Run it**

Run: `python3 scripts/verify_backup_run.py` (Linux, with the compose stack up)
Expected: the write assertions still pass and the new restore assertions pass.
**Paste the actual output into the commit message** — this is a measurement, not
a claim. If the stack cannot be brought up in this environment, say so in the
report rather than reporting the leg as verified.

- [ ] **Step 3: Commit**

```bash
git add scripts/verify_backup_run.py
git commit -m "test(backup): the live measurement now restores what it wrote"
```

---

### Task 10: Close the item in the docs

**Files:**
- Modify: `docs/modules/backup/spec.md` (§6.1 still-open item 1)
- Modify: `docs/modules/backup/agent-engine.md`
- Modify: `docs/kernel/wire-protocol/README.md`

- [ ] **Step 1: Close item 1**

Rewrite §6.1's item 1 from **STILL OPEN** to closed, stating what was built.
Keep the superseded text quoted and dated, as every other correction in that
file does. Record that relay-through-the-gateway — which that section called
"the strongest candidate" — was deliberately overridden, and why: its stated
side-benefit is recorded as moot in the same paragraph, and it would put every
restored byte across a websocket sized for job messages.

- [ ] **Step 2: Record the two new kinds in the wire-protocol README**

`backup_read_query` and `backup_read_grant`, with the compatibility note: an old
control plane rejects an unknown kind at `parse_agent_message`, so a new agent's
restore fails on the unanswered query with a stated reason rather than hanging
or half-restoring. Record the divergence from the write half in the same terms
the existing prefix-grant note uses — the write grant is one policy over a
prefix; a read is signed per object because SigV4 query signing cannot express
the prefix form.

- [ ] **Step 3: Update `agent-engine.md`**

Its restore section describes a capability that cannot read. State what it does
now: batches of ≤256 suffixes, a 900-second grant, sequential fetch, bounded
retry at 3 attempts — and the two known limits from spec §7, unvarnished:
intra-org cross-point reads are not prevented, and restore is single-threaded.

- [ ] **Step 4: Check no stale claim survives**

Run: `grep -rn "cannot read from the repository\|READS ARE NOT\|SigV4 query signing cannot" docs/ agent/ backend/`
Expected: hits only inside quoted, explicitly-superseded text, or inside a
sentence that says why reads are signed per object rather than saying they are
impossible.

- [ ] **Step 5: Commit**

```bash
git add docs/modules/backup/spec.md docs/modules/backup/agent-engine.md \
        docs/kernel/wire-protocol/README.md
git commit -m "docs(backup): restore works now; close §6.1 item 1"
```

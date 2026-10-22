# Design — the backup transport: closing spec §6.1 items 2 and 3

**Status:** approved in brainstorming, not yet implemented.
**Scope:** make `backup.run` complete end to end.
**Out of scope:** `backup.restore`, which needs prefix-scoped READ — its own cycle.

Read `CLAUDE.md` first; the six invariants govern this work, and invariant #3
(no stored credential on an endpoint) is the reason this design exists at all
rather than the simpler one.

---

## 0. Why this cycle, and what it is not

`docs/modules/backup/spec.md` §5.2 moved the dedup check off the endpoint and
onto the control plane, which is what made `backup.run` buildable without giving
an endpoint an object-store credential. The **server half of that shipped**:
`app/gateway/protocol.py` defines both message kinds, `dispatch.py:382` routes
the query, `app/modules/backup/dedup.py::handle_chunk_query` answers it, and
migration `0053_backup_chunk_index` holds the index.

The **endpoint half did not**, and `backup.run` therefore does not work. Three
blockers, each verified in code rather than inferred:

| # | Blocker | Evidence |
|---|---|---|
| 1 | Wire framing missing | `agent/internal/wire/wire.go:157` — `Decode` rejects every inbound kind but `job`; no encode path for `backup_chunk_query` |
| 2 | `OpenStore` nil in production | `backup.go:213` registers `Process = &Service{}`; `backup.go:252` refuses on nil |
| 3 | `QueryChunks` nil in production | `dedup.go:346` errors on the nil seam |

These are spec §6.1's "Still open after §5.2" items 2 and 3. Item 1 of that list
— prefix-scoped READ, the restore side — is **not** addressed here.

**This cycle is ordered before restore deliberately.** A batched READ-grant
message kind needs the same framing work as item 2, so framing is a prerequisite
for both, and building restore first would mean building a message kind the
agent cannot receive.

---

## 1. Resolved decisions

Each was a real choice with a rejected alternative. Recorded so a later session
does not silently re-litigate one.

### 1.1 Correlation lives in its own package, not in the transport

`transport.Run(ctx, opts, handler func(context.Context, []byte))` is strictly
one-way. The dedup exchange is request/response: a flush must block until the
grant bearing **its** `query_id` returns. Nothing in the agent correlates a
reply to a request today.

**Chosen:** a small `agent/internal/chunkreply` package holding
`map[queryID]chan wire.ChunkGrant` behind a mutex, composed into the inbound
handler in `main.go`. Zero goroutines and zero allocation until a query is in
flight. It follows the wiring conventions `main.go` already established with
`outbox.Attach(ctx, conn)` and `logs.Process.Bind(...)`.

**Rejected — a generic `Conn.Request(ctx, msg, replyKind)`:** it pushes
application concepts (correlation ids, reply kinds) into a transport that
carries the mTLS identity and the reconnect logic and is heavily tested.
Reusability for a single caller does not pay for that blast radius.

**Rejected — a reply table on `runner.Handle`:** a grant is not a job, and the
runner would take on a lifetime and a concern it does not have.

**One transport change is nonetheless required, and it is deliberately the
smallest one.** `transport.Options` has `OnConnect`, `OnConnected` and
`OnUnauthorized`, but **no disconnect hook**, so "pending waits fail
immediately" (§4) is not expressible today. This adds
`OnDisconnect func(context.Context, error)`, called in `Run` after
`conn.serve` returns and `conn.Close()` runs. It follows the three existing
hooks exactly, including their compatibility rule — an additive Go field, not a
wire change, where a nil hook is the previous behaviour precisely.

This is not the rejected `Conn.Request`: a disconnect notification is a
*transport* fact the transport already knows, whereas correlation ids and reply
kinds are application concepts. The transport learns nothing about backup.

### 1.2 The message types live in `wire`, not in `capabilities/backup`

`ChunkQuery`, `ChunkGrant` and `UploadGrant` are currently declared in
`capabilities/backup/dedup.go`. They **move to `agent/internal/wire`**.

This is the existing convention, not a new one: `capabilities/logs` and
`capabilities/health` both import `wire`, build wire types directly, and take
the transport as an injected seam (`Options.Sender`). Moving them gives one
definition instead of two plus a converter, and puts them where
`wire/schema_test.go` can pin them to the exported JSON schemas.

~55 references move across 7 files; 5 are backup's own tests. Mechanical.

### 1.3 The server derives `manifest_ref`; the agent returns a suffix

`get_backup_object` requires `s3://{bucket}/{key}` (`app/core/storage.py:456`),
and `ObjectStore`'s contract says the agent does not know the bucket — a POST
grant does not tell it. So there is currently **no correct value for `Put` to
return**.

**Chosen:** `Put` returns the relative suffix (`manifests/{name}`);
`app/modules/backup/dispatch.py` validates it and derives the full ref with the
same `_backup_key` + bucket it already uses. No wire change. Key construction
stays server-side — the "derived, never accepted" rule `presign_backup_object`
is already built on. The validator exists and rejects `..`, absolute paths,
backslashes and empty segments.

**Timing matters and will not hold:** `backup.run` has never completed, so there
are **zero `BackupPoint` rows in existence**. Changing what `manifest_ref` means
is free today and needs a data migration once the first real backup lands.

**Rejected — the agent parses the bucket from `grant.URL`:** works only because
the Option 1 route made the path prefix *be* the bucket name. That is a Traefik
routing decision; baking it into every endpoint means it breaks silently if the
route changes, and the endpoint starts constructing a key the server trusts.

**Rejected — a `ref_prefix` field on `BackupUploadGrant`:** a schema change to a
shipped, exported wire contract, and it hands the endpoint a bucket name it has
no other use for.

**Cost, stated:** this touches `dispatch.py`, which is shipped server code, and
`Put`'s return value stops being a full ref — its interface comment must be
rewritten to match rather than left to mislead.

### 1.4 The upload client uses system trust roots

Every byte the agent sends today goes to the gateway on `:8443`, authenticated
by a certificate **pinned to the internal CA at enrollment**. The POST grant
sends it to `:443`, carrying the operator's publicly-trusted certificate, which
the internal CA cannot validate. This introduces a **second trust domain on the
endpoint** and is decided rather than defaulted.

**Chosen:** system trust roots, for the upload client only. The gateway's
internal-CA pinning is untouched and the two clients share no TLS config.
Additionally: `https://` enforced (a plaintext grant URL is refused outright),
redirects never followed (a `302` turns a fixed-host client into an
arbitrary-host one — the reasoning `ai-admin` §17.5 records), and every request
deadline-bounded.

**Residual risk, bounded and stated:** a machine with a hostile root CA
installed could MITM chunk uploads. It sees **ciphertext** — the payload is
sealed with the repository key before it leaves — so it can withhold or corrupt
an upload, not read it. *That property is what makes this acceptable, so it must
not be "simplified" later by moving encryption server-side.*

**Rejected — pinning the internal CA for uploads:** the `:443` certificate is
not internal-CA-issued, and routing object storage through the gateway's
certificate is explicitly forbidden (agents pin the internal CA at enrollment).

**Rejected — an optional operator-supplied extra root:** YAGNI until a
deployment whose storage route is not publicly trusted actually exists.

### 1.5 `backup.restore` keeps refusing — the refusal moves

Once `OpenStore` is non-nil, restore **stops refusing at `openStore`** and would
proceed to fail somewhere less legible. So `backupstore`'s `Get`/`GetRef`
themselves return `jobs.Refuse(...)` naming prefix-scoped READ as the unsolved
gap. The honest refusal is relocated, not removed, and the next cycle replaces
exactly those two methods.

---

## 2. Components

Five units.

**1. `agent/internal/wire`** — framing, and it owns the message types (§1.2).
Adds the 7th outbound kind (`backup_chunk_query`) and the second inbound kind
ever (`backup_chunk_grant`). `Decode` already returns `any`, so it gains a
second return type rather than a new signature, and keeps refusing everything
else. `schema_test.go` pins both to the exported JSON schemas and corrects the
stale `notImplemented` map, which still claims "no agent capability planned" for
`Backup*`, `Patch*`, `AppDeploy*`, `ScriptRun*` and `Provision*` — all built.

**2. `agent/internal/chunkreply`** — the correlation registry (§1.1). A
`Wait`/`Deliver` pair, a bounded pending count, and a disconnect hook.

Two constants, pinned here so they are not guessed at implementation time:

- **`maxPending = 16`.** Only the agent adds entries, so the map is already
  self-bounding — but a cap turns "a bug spawned unbounded concurrent flushes"
  into an immediate, named failure rather than quiet memory growth on someone's
  desktop. One run flushes serially, so the realistic pending count is 1.
- **The registry keys on the pair `(job_id, query_id)`, not `query_id` alone.**
  `dedup.go:196` already mints `query_id` as a per-session counter (`"q1"`,
  `"q2"`, …) and `dedup.go:204` already validates that a grant echoes **both**
  `query_id` and `job_id`. The counter is correct within a session but restarts
  at `q1` for every run, so a registry keyed on `query_id` alone could hand a
  late grant from a dead run to a new run's identically-numbered flush. Keying
  on the pair closes that without changing minting code that is already tested,
  and it reuses the two fields the capability already checks.

**3. `agent/internal/backupstore`** — the POST-grant `ObjectStore`. `Put` builds
the multipart form from `grant.Fields` plus `key` and POSTs to `grant.URL` under
the TLS rules in §1.4. The client is constructed at job time and closed with the
job. `Get`/`GetRef` refuse (§1.5).

**4. `agent/cmd/agent/main.go`** — wiring only. Composes the inbound handler
(grant → registry, everything else → `runner.Handle`) and calls
`backup.Process.Bind(...)`.

**5. `backend/app/modules/backup/dispatch.py`** — the `manifest_ref`
normalization (§1.3).

---

## 3. Data flow

One `backup.run`. Steps 4–7 are the loop; the rest happen once.

1. The control plane dispatches the signed job. `wire.Decode` → `*Envelope` →
   the runner verifies the Ed25519 signature → `backup.Process.Run`
   (invariant #1, unchanged).
2. `Service` resolves `openStore` and `queryChunks` **at job time**. Nothing was
   allocated while idle.
3. Snapshot taken; the writer walks the tree, chunking and computing BLAKE3 ids.
4. Chunk **references** — id, snapshot path, offset, length, ~100 bytes —
   accumulate to at most 1024. Never plaintext: that is what keeps the footprint
   flat regardless of data volume.
5. **Flush.** Mint a `query_id`, register it with `chunkreply`, send
   `wire.ChunkQuery{job_id, query_id, check[], stored[]}`, block on
   `{grant, ctx.Done(), deadline}`.
6. The **server** resolves org, device, repository and prefix *from the job it
   signed* — the query names only a `job_id` — partitions `check`, records the
   previous round's `stored` ids, mints a grant with `presign_backup_write`, and
   replies. The inbound handler decodes it; `chunkreply.Deliver(query_id)` wakes
   the flush.
7. For each id in `new`: **re-read** from the snapshot at `(path, off, len)`,
   **re-hash, and fail the run on mismatch** — different bytes under a
   content-addressed id would corrupt every backup point sharing that chunk.
   Then seal with the repository key and `Put` under `chunks/{id[:2]}/{id}`.
   Uploaded ids become the next query's `stored[]`.
8. The manifest is sealed and `Put` under `manifests/{name}`; `Put` returns the
   **suffix**.
9. `BackupRunResult{state, manifest_ref: "manifests/{name}", …}` travels back as
   an ordinary `wire.JobResult`.
10. `dispatch.py` validates the suffix, derives
    `s3://{bucket}/{org_id}/backup/{repository_id}/manifests/{name}`, and writes
    the `BackupPoint`.

Three properties fall out of this shape and are load-bearing:

- **Authorization rides the dedup answer.** The grant arrives in the same
  message as `new`, so writes cost no separate authorization round trip. That is
  what makes the ~1/1024-of-a-round-trip-per-chunk figure real.
- **Long runs renew their own grant.** Every flush returns a fresh grant with a
  fresh `expires_at`, so a multi-hour backup never needs a long-lived
  credential. The window stays short *because* the exchange is chatty.
- **The server never accepts a key from the endpoint.** The agent sends ids
  matching `CHUNK_ID_PATTERN` and a suffix; the server derives every object key.

---

## 4. Error handling

The governing fact: **silence is the refusal.** Every authorization failure
produces no reply at all (`protocol.py:259`), because a distinguishable error
would be an oracle a compromised endpoint could walk. The agent can therefore
never wait unbounded.

| Failure | Detection | Response |
|---|---|---|
| Server refuses the query | **silence** | Deadline expires → run **fails** naming the unanswered query |
| Connection drops mid-wait | registry disconnect hook | Pending waits fail **immediately** — a reply cannot arrive on a new connection |
| Grant with an unknown `query_id` | not in the pending map | Dropped, logged once — a late reply after a timeout |
| Second grant for one `query_id` | entry already consumed | Dropped |
| `new` ⊄ `check` | subset check on arrival | Run **fails** |
| Grant expired before a `Put` | S3 `403` | Run fails; the grant is never retried |
| Key outside the signed prefix | S3 `403` | Run fails **loudly** — an agent bug, not a transient |
| Body over `content-length-range` | S3 `400` | Run fails naming the cap |
| Transient network on `Put` | transport error | **Bounded retry with backoff**, then fail |
| Re-read hash mismatch | agent re-hash | Run fails (already built) |
| Run dies after upload, before reporting `stored[]` | — | Index never learns; **GC reconciliation repairs** (`957e9d5`) |
| Run dies mid-way | — | Orphan chunks; **GC reclaims** what no manifest references |

Three decisions inside that table:

- **Retry on `Put` is safe because the store is content-addressed.**
  Re-uploading identical bytes under a BLAKE3-derived key is idempotent. The
  manifest's name is randomly minted, but a retry within one job writes the same
  sealed bytes to the same name, so it is equally safe. **3 attempts total,
  backing off 1s then 2s**, and only for transport errors — a `403` or `400` is
  a verdict from the signed policy and is never retried. Retries stay small
  deliberately: this is an endpoint on someone's desk, not a batch job.
- **The `new` ⊄ `check` check is cheap hardening.** The agent already holds
  `check`; verifying the answer is a subset costs one membership pass and closes
  the only way a reply could steer the agent toward reading data it was not
  already reading.
- **The deadline is injectable via `Options`**, so tests drive it to
  milliseconds rather than sleeping. Default 60s — generous for an index lookup,
  short enough that a refused job fails visibly rather than looking hung.

A grant is a bearer credential: never logged, never in a job result, dead with
the job (`redact.go` already covers `UploadGrant`). The agent never reports a
`stored` id it did not upload; the residual risk of a tenant poisoning its own
dedup index is bounded to that tenant and repaired by GC, exactly as §5.2
accepts.

---

## 5. Testing and verification

### 5.1 Unit tests

- **`wire`** — round-trips both kinds; refuses an unknown kind, a `v:2` frame,
  and an outbound kind arriving inbound. `schema_test.go` pins the types to the
  exported schemas, including `maxItems: 1024` → `maxChunkBatch`, so the bound
  lives in a test rather than a comment.
- **`chunkreply`** — correlation by id; unknown dropped; duplicate dropped;
  deadline fires; disconnect fails pending waits immediately.
- **`backupstore`** — the form carries `grant.Fields` plus `key`; `http://`
  refused; redirect refused; deadline honoured; `Get`/`GetRef` refuse.
- **`capabilities/backup`** — the existing suite, updated for the moved types.
- **Python** — `dispatch.py` suffix validation rejects `..`, absolute paths,
  backslashes and empty segments, and derives the correct ref; the existing
  dedup suite stays green.

### 5.2 Budget tests, built the way the traps require

`go test -bench` **exits 0 on a regression** — measured on this project: a
planted 64 KiB/op regression printed `65536 B/op` and passed. So budget
assertions live in an ordinary `Test` calling `testing.Benchmark(…)` and
comparing.

An after-the-fact RSS or retained-heap reading **cannot see a transient peak**,
so these gate on **cumulative allocation**: a run's allocation must not scale
with data volume — `budget_test.go`'s existing falsifier 3 — and it must still
hold once a real transport replaces the injected seam.

### 5.3 Against fixture blindness

This project's defining defect is a guard test whose fixture makes the guarded
and unguarded paths produce the same observable. Therefore:

- Every control (subset check, `https` enforcement, redirect refusal, deadline,
  prefix check) gets a **falsifying mutation**.
- Each mutation is verified to have **applied** (`assert new != old` on the
  edited text) before the run — a find-and-replace matching nothing passes and
  reads as proof.
- Guards assert the **state of the world** as a tuple, never "did not raise".

### 5.4 The live measurement

A script under `scripts/`, following `demo_e2e.py`'s precedent, run **manually**
against the real compose stack — Traefik and MinIO, not `moto`:

1. Real org, real enrolled device, real repository, real signed `backup.run`.
2. Assert the exchange crosses the wire; chunks land under
   `{org}/backup/{repo}/chunks/…`; the manifest lands; the `BackupPoint` row's
   `manifest_ref` is fetchable via `get_backup_object`.
3. **The two negatives `moto` cannot express** — measured, not assumed: `moto`
   accepts an upload whose key violates the signed policy and answers `204`.
   Against real MinIO, a key outside the prefix must be `403` and a body over
   `content-length-range` must be `400`.
4. **A second run over unchanged data → near-zero new chunks.** Without this,
   every assertion above passes on an exchange that dedups nothing.
5. Report plainly what was and was not driven end to end.

### 5.5 What this does not prove — state it, do not let green tests imply it

- **CI does not guard the live measurement.** CI's `agent` job does run
  `agent/`'s unit, schema and idle-budget suites, with a real Go toolchain, on
  every push — that job predates this cycle. What it does not run is
  `scripts/verify_backup_run.py`, the live measurement against real MinIO: the
  script is manual, so a regression in what it checks can return silently.
- **This measures Linux only.** No Windows or macOS endpoint has ever run the
  agent, and this cycle does not change that.
- **The compose certificate is not the operator's real public certificate**, so
  §1.4's system-roots decision is exercised, not proven.

---

## 6. Related documents

- `CLAUDE.md` — the six invariants and the agent idle budget.
- `HANDOFF.md` — the traps, the operator's settled decisions, the open items.
- `docs/modules/backup/spec.md` §5.2 (the exchange) and §6.1 (the five
  operations, and which remain open). **§6.1 must be amended when this lands.**
- `docs/kernel/wire-protocol/BackupChunkQueryMsg.schema.json`,
  `BackupChunkGrantMsg.schema.json` — the pinned contract.
- `backend/app/gateway/protocol.py`, `app/modules/backup/dedup.py` — the server
  half, already built.
- `docs/modules/app-deploy/spec.md` §5.2 — the presigned-URL shape that fits one
  known key, and why backup could not reuse it.

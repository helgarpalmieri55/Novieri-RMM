# Prefix-scoped READ for `backup.restore` — design

**Date:** 2026-08-13
**Module:** `backup` (`docs/modules/backup/spec.md` §6.1, still-open item 1)
**Closes:** the last open item that makes a shipped capability non-functional.

---

## 1. The problem, stated exactly

`agent/internal/backupstore`'s `Get` and `GetRef` both `jobs.Refuse`. Their
refusal text is accurate and worth keeping in mind:

> writes travel under the prefix-scoped POST grant the control plane returns on
> the dedup exchange, but SigV4 query signing cannot express a prefix-scoped
> READ, and a restore's chunk selection can run to six figures — one presigned
> URL each is tens of megabytes of job params.

So the backup module can **take** backups and cannot **return** them. For a
backup product that is the failure mode that matters: everything works until the
day someone needs their data.

Restore reads exactly two things (`restore.go`): the manifest once, via
`GetRef`, and then chunks in a loop, via `Get`. Only the second is a
six-figure problem. At the pinned average chunk size of 1 MiB
(`DEFAULT_DEDUP.avg = 1048576`), a 100 GB restore is ~100,000 chunks and a 1 TB
restore ~1,000,000.

---

## 2. Decisions

### 2.1 Batched READ grants, not relay through the gateway

`spec.md` §6.1 named relaying object bytes through the gateway as "the strongest
candidate". **Overridden deliberately**, for two reasons that were not true or
not weighed when that line was written:

- Its stated advantage — that it "would also have dissolved item 5 as a side
  effect" — is recorded in the same paragraph as **moot**, because item 5 was
  fixed another way.
- It would put bulk data on the control plane. A 100 GB restore means 100 GB
  across a websocket sized for job messages, making the control plane a
  bandwidth bottleneck and a single point of failure for every restore in the
  fleet.

The agent **already reaches the object store directly** — that is how writes
work today, measured against real MinIO behind real Traefik. Relay would route
around a path already proven to work.

Cost of the chosen route: a new wire kind pair and a URL-expiry story. Both are
below.

### 2.2 A grant is scoped to the job's own repository prefix

Chunk ids are `BLAKE3-256(plaintext)`, so **a chunk id is guessable by anyone
who knows the content**. The authorization rule is therefore the whole security
boundary of restore.

Every key in a query must resolve under `{storage_ref}/` for the job's own
repository, taken **from the job record on the server**, never from anything the
agent sent. Dedup is already per-repository, so a chunk belonging to another
tenant does not exist under this prefix and no signature can reach it.

**Rejected — also verifying each id against the point's manifest.** Stricter: it
would stop a compromised agent reading chunks from a *different backup point of
its own org*. It costs a manifest read plus set membership per batch, and the
manifest is encrypted, so the server would need either the key or a separate
chunk index. Recorded as a known limit in §7; the cross-tenant boundary — the
serious one — is closed by prefix scoping.

### 2.3 Short expiry, just-in-time batches, re-request on failure

Grants are requested only when about to be used, and expire in minutes rather
than hours. **A leaked URL is worth what its remaining lifetime is worth**, so
the window stays small; and because chunk reads are idempotent, an expiry is
not a condition to engineer around — the batch is simply re-requested.

**Rejected — long expiry with batches pipelined ahead.** Faster on high-latency
links, but a leaked URL stays usable for hours and the agent must hold many URLs
resident, against this module's footprint discipline.

**Rejected — expiry tied to the job's `expires_at`.** Tidy, but a job with a
generous `expires_at` silently widens the signing window, which is the property
being controlled.

### 2.4 A new wire kind pair, not a mode field on the existing one

`docs/kernel/wire-protocol/README.md` and CLAUDE.md both treat a wire change as
a compatibility decision. A running fleet is always mixed-version.

New kinds: `backup_read_query`, `backup_read_grant`.

**Rejected — reusing `backup_chunk_query` with a read/write discriminator.**
Inbound parsing is deliberately loose, so an OLD server would **ignore** the
unknown field and reply with a WRITE grant. A new agent could then misread a
write authorization as a read one. That is a silent wrong answer where the new
kind gives a loud failure: an old server rejects an unknown kind, the agent's
pending wait fails, and `backup.restore` refuses with a stated reason.

**Rejected — adding protocol version negotiation.** Cleanest long-term, but the
protocol has none today and this feature does not need it built.

---

## 3. The wire contract

```
agent → server   backup_read_query   { request_id, job_id, suffixes[≤256] }
server → agent   backup_read_grant   { request_id, grants[{suffix, url, expires_at}] }
                                     | { request_id, refused: "<stated reason>" }
```

**They are SUFFIXES, relative to the repository prefix — never full keys.** The
server joins each onto `{storage_ref}/` itself. This is deliberately tighter
than accepting full keys and validating them: an agent cannot even *express* a
key outside its own repository, so the check is a property of the construction
rather than of remembering to validate.

Two things the server must still reject, because a relative path can escape:

- any suffix containing a `..` segment, a leading `/`, a backslash, or a NUL —
  the same alphabet `path.go` already refuses in manifest entries, and for the
  same reason: evidence the manifest or the store is compromised;
- any suffix that does not resolve, after cleaning, to a path still under
  `{storage_ref}/`.

Either one refuses the whole batch (§2.2).

`Decode` returns `*ReadGrant` for the inbound kind, exactly as it already
returns `*ChunkGrant`; `main.go` routes it to a `readreply` registry keyed by
`request_id`. Job envelopes and verification are untouched.

**One mechanism serves both reads.** `GetRef` is a query with one suffix;
`Get(chunkSuffix)` is a query with up to 256. `manifest_ref` is derived
server-side, so the agent sends the manifest's suffix under the same prefix
rule as any chunk — there is no path where the agent names an absolute location. The manifest gets no special
path — a second path is a second thing to get wrong, for no benefit.

---

## 4. Batch size, and why it is not the write side's number

The write exchange batches **1024 ids**; no bytes move, so the number is free.

Here every grant is ~1 MiB to fetch. 1024 keys is ~1 GiB per batch, which on a
10 Mbps link is ~14 minutes of transfer — long enough that URLs expire mid-batch
and the restore thrashes.

**256 keys (~256 MiB) with a 15-minute expiry.** On the same 10 Mbps link that
is ~3.5 minutes of transfer inside a 15-minute window. Erring small is safe
because re-requesting is cheap; erring large strands work.

*(Both figures are arithmetic from the pinned 1 MiB average chunk size, not
measurements. The live measurement in §6 is what will confirm the window is
comfortable in practice.)*

---

## 5. Failure, retry, and structure

**Three failures, one response.** A batch fails on URL expiry, on a dropped
correlation (`chunkreply.Detach` fails pending waits on disconnect, and the read
registry mirrors that), or on a transient store error. All three re-request the
batch, bounded; a batch that keeps failing fails the restore with the reason
rather than looping.

**Structure — mirror the write side, do not generalise it.** A `readreply`
registry beside `chunkreply`, a `ReadGrant` type beside `ChunkGrant`. The write
path is measured and proven; a regression in a shared registry would break
*backup*, not merely restore. This follows the convention stated at
`internal/capabilities/netsweep/netsweep_test.go:1240`, and the two halves
genuinely differ: a failed write retries against a fresh grant, while a read
must never silently substitute one chunk for another.

**Sequential fetch, deliberately.** Parallel GETs would restore faster, but
concurrency is a footprint claim and this module measures those. Sequential
first; parallelism is its own cycle with its own budget falsifier, not smuggled
in here. Stated as a known limit rather than left to be discovered.

**Transport policy is the write path's, unchanged:** https with system trust
roots, plaintext refused, redirects refused.

---

## 6. Verification

- **Unit tests** drive a fake grant provider through every branch: a grant that
  expires before use, a refused batch, a dropped correlation, a key outside the
  prefix, a short/truncated object, and a batch boundary that lands exactly on
  256.
- **Server-side tests** assert the authorization rule directly: a key outside
  the job's `storage_ref` refuses the WHOLE batch, and the refusal names the
  reason without echoing the offending key into the audit log.
- **The live measurement is extended.** `scripts/verify_backup_run.py` already
  proves the write grant against real MinIO. It gains a restore leg: write a
  point, restore it, compare bytes. This is not optional polish — §6.1 records
  that `moto` accepts a policy-violating key and answers `204`, so every unit
  test of the grant asserts the signed policy document and nothing else. **A
  restore that only passes against a fake proves the same nothing.**

Falsifiers for this design's own claims:

| claim | falsifying observation |
| --- | --- |
| A grant cannot reach outside the job's repository | a signed URL resolving to a key not under `{storage_ref}/` |
| An expired URL costs a re-request, not a failed restore | a restore failing when a batch expires mid-fetch |
| An old server produces a loud failure | a restore that hangs, or half-restores, against a server that does not know the kind |
| The manifest needs no special path | a `GetRef` that cannot be served by a one-suffix query |
| An agent cannot express a key outside its repository | a query whose suffix, after cleaning, resolves outside `{storage_ref}/` and is still signed |

---

## 7. Known limits, stated rather than discovered later

- **Intra-org, cross-point reads are not prevented.** Prefix scoping stops
  cross-tenant access; a compromised agent could still request chunks belonging
  to a different backup point of its own organisation, if it knew their ids.
  Closing that needs a chunk index or a manifest membership check (§2.2).
- **Restore is single-threaded** and will be slower than the network allows.
- **The 256/15-minute figures are reasoned from the pinned chunk size**, not
  measured. §6's live leg is what will confirm them.

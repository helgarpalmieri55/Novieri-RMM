# Handoff — Toeshee-RMS, continuing in a new session

**Refreshed 2026-08-16.** Everything through PR #45 is merged to `main`
(`32b17bb`). **Read `CLAUDE.md` first.** It is the contract, not background
reading: six security invariants, the build discipline, and the agent idle
budget.

**This file rots.** It was found badly stale on 2026-08-16 — it still said no
snapshot provider existed and that prefix-scoped READ was unsolved, both of
which cycles had closed weeks earlier, and the README had the same disease
(claiming the Go agent was "not built" against ~68k lines of Go). §8 says the
repo is the authority; act on that before trusting any sentence here.

This document is the state of play and the traps. It does not repeat what the
specs already say — those live in `docs/` and are authoritative.

---

## 1. Where the work stands

Every roadmap phase is built (kernel through Phase 18), plus Reporting, Site
Servers, AI Automation, and the Go agent in-tree with 15 of 16 job types
implemented on both sides — the server sends `remote.session.open`, and the
agent has no handler for it (`agent/internal/wire/schema_test.go:116` says so
directly: "no agent capability planned"). Remote control's Start Session
button ships in the console anyway; see
`docs/reviews/2026-09-23-platform-critique.md` #2. A four-part observability
and operations programme
also landed (logging kernel, platform health and status, error monitoring,
deploy rollback) — `docs/08-observability.md` is the reference and
`docs/product.md` is the measured "what this is".

Verify the state yourself before trusting any of this:

```sh
cd backend && ./.venv/bin/pytest -q     # 6170 passed, 0 failed, ~25 min
cd agent   && go test ./... -race -count=1
cd frontend && npx vitest run           # 509 passed
```

**The backend suite runs in DETERMINISTIC file order.** `pytest-randomly` is
not in `pyproject.toml`'s dev extras and not installed, so `-p no:randomly` is
a silent no-op and a green run says nothing about order independence. Do not
describe a green suite as "passing under random ordering" — that claim was
made twice on 2026-08-16 and was wrong both times. See §6.

**Postgres is not always running.** `service postgresql start`, then
`redis-server --daemonize yes` if `redis-cli ping` fails. A dead Postgres makes
unrelated tests look broken — the errors are `CalledProcessError` from
`sudo -u postgres psql`, not real failures.

---

## 2. The backup path — what is built, and what has never run

Kept because it is the most-misread part of this project, and because each
correction below is legible in place. It was written as "in flight when this
session ended"; those sessions are long over and their work is merged. Read it
as the standing record of the backup storage path, not as a to-do list.

**The distinction that keeps being lost: `built` ≠ `measured` ≠ `completes end
to end`.** All three appear below, about the same subsystem, and they are not
the same claim.

### Option 1 — route object storage on :443 — **DONE**

`682f108`, `8665bb9`, `8c75de9`, `ccf7392`, `aa7aeb1`, `977eb6a`, `65e0c5a`,
`f834233`, `22a034a`.

The defect: MinIO had no published port and no Traefik router, and presigned
URLs were generated against `http://minio:9000`, a Docker-internal hostname. So
**every presigned URL the platform produced was unroutable from an endpoint** —
this already broke `app.deploy`'s artifact fetch, which had shipped believing it
worked. **`app.deploy` now works end to end.**

The route: a path prefix on the operator's hostname **where the prefix IS the
bucket name** — `https://itms.toeshee.com/it-suite/<key>`. No new DNS, no SAN,
and deliberately **no `stripPrefix`**, because that URL is already exactly S3
path-style addressing, so nothing is rewritten and the signature covers the path
MinIO receives. Both alternatives were measured and both fail invisibly from the
control plane: strip a foreign prefix → `SignatureDoesNotMatch`; keep one →
path-style reads segment 1 as the bucket → `NoSuchBucket`. `addressing_style` is
pinned to `"path"` so a botocore default cannot drift into virtual-host style and
demand a DNS name the operator does not have.

`ITS_S3_PUBLIC_ENDPOINT` is used **only** for signing; `ITS_S3_ENDPOINT` still
carries all api/worker I/O. Unset in production, `ObjectStore` refuses to
construct — it does not fall back. Plaintext http and any endpoint carrying a
path are refused too.

Verified against real MinIO behind real Traefik: presigned GET/HEAD 200 with
bytes matching; anonymous GET 403 (a bucket from `_ensure_bucket` has **no
policy at all**, and MinIO's default for that is deny); expired URL 403 `Request
has expired`, evaluated on receipt so a download that starts in time completes;
listing, signed PUT/DELETE and the console never reach MinIO; the backup POST
policy is enforced — 403 outside the prefix, 400 over the size cap.

**Two things to know.** Blocked requests return **200 and index.html**, not 404 —
the `web` catch-all (`PathPrefix(/)`, priority 1) answers before Traefik would.
The security property is unchanged (MinIO never sees them) but the observable is
not a status code, so the go-live check greps for `<ListBucketResult>`. And a
leaked `ITS_S3_SECRET_KEY` still permits listing through the route, because the
route cannot distinguish a valid signature from a stolen one — GO-LIVE flags that
key as fleet-wide rather than a storage detail.

### Option 7 — server-side dedup — **THE GRANT IS PROVEN; `backup.run` HAS STILL NEVER COMPLETED**

`f031bad` (migration `0053_backup_chunk_index`), `574f78b`, `2be69bf`, `957e9d5`,
`c4ec718`, `f66808c`, `db3d718`.

The agent sends its chunk hash list; the server answers which are new **and
presigns PUT grants for exactly those** in the same response, routed down the
device's own gateway channel. `BackupChunkQueryMsg` / `BackupChunkGrantMsg` are
in `app/gateway/protocol.py` with `MAX_CHUNK_BATCH = 1024` and a
`CHUNK_ID_PATTERN` that is a **key-safety control**, not cosmetic: a chunk id is
concatenated into an object key and rides into a presigned grant.

`957e9d5` reconciles the dedup index with GC — without it the exchange would
eventually tell an agent "you already have that" about a chunk GC had deleted.

**Three of the five storage operations are now closed** — dedup `Has`, chunk
`Put`, manifest `Put` — all under the prefix-scoped POST grant, with **no
credential on the endpoint**. See `docs/modules/backup/spec.md` §5.2 and the
amendments at the head of §6.1.

**The grant was then MEASURED**, by `scripts/verify_backup_run.py`, against
real MinIO behind real Traefik on `:443` with a real compiled agent binary and
a real signed job (Linux, manual, not in CI). Against a real object store:
**403** for a key outside `{storage_ref}/`, **400** for a body over the signed
`content-length-range`, **204** for a compliant POST under the same grant. That
last one is not garnish — without it the two rejections are equally consistent
with a route that refuses everything. This is the enforcement `moto` cannot
express (see §4).

**That run established that `backup.run` DOES NOT complete end to end**, for
two reasons that are not the storage transport and that no test in this tree
could see at the time. One of the two is now fixed; the other is not.

1. ~~**No snapshot provider exists.**~~ **CLOSED (snapshot-provider cycle).**
   The original text: "`main.go` leaves `backup.Options.OpenSnapshot` nil and
   `defaultOpenSnapshot` refuses `vss`, `lvm` AND `apfs` — the long-stated
   BK-01 gap. `run.go` resolves the snapshot BEFORE it opens the store, so a
   stock binary answers every `backup.run` with `status=refused` on every
   platform, before one chunk query leaves the machine."

   `main.go:328` now binds `OpenSnapshot: backup.PlatformSnapshot`, and
   providers exist for all three platforms (LVM, APFS, VSS). **This narrowed
   the gap; it did not close `backup.run`.** Nobody has observed a
   `backup.run` complete end to end on any platform — see §5 and
   `docs/modules/backup/spec.md` §6.1 item 4, which is scrupulous about the
   difference between "a provider exists" and "a backup happens".
2. **The dedup exchange deadlocked against the agent's own transport.**
   **FIXED, this cycle**
   (`889719d`, `docs/superpowers/specs/2026-08-09-agent-job-concurrency-design.md`).
   `jobs.Runner.Handle` used to run on the transport's READ goroutine and
   block for the whole capability — deliberate, so job ordering was the
   wire's ordering. §5.2's exchange needed the opposite: `backup.run` blocked
   waiting for a grant on the connection whose only reader was the goroutine
   it was occupying. Measured with a build that got past (1): the server
   answered and published the grant, it sat unread for the full 60s
   `chunkreply` deadline, the run failed, and only then did the agent read it
   and log `dropped a backup chunk grant that nothing was waiting for`. The
   blocked reader also sent no WebSocket pong, so the gateway closed the
   connection at the same moment. **Every test of this exchange injected
   `Options.QueryChunks` and answered it in-process**, which is why it was
   invisible.

   **The fix:** `Handle` still decodes, verifies and looks up the job inline,
   in wire order, but now submits to a `Dispatcher`
   (`agent/internal/jobs/dispatcher.go`) and returns immediately — capability
   execution moved to per-capability worker goroutines, concurrent across
   capabilities, strictly serial within one, with a heavy budget of 2 for
   `{backup, patch, app, script}` and a bounded queue (depth 8) per
   capability that refuses visibly instead of blocking or growing unbounded.
   Both numbers are reasoned defaults, not measured ones.
   The read goroutine is free the moment `Submit` returns, so it is there to
   read the grant.

   **Proven, not asserted.** A new harness
   (`agent/internal/capabilities/concurrency_harness_test.go`) composes the
   real read loop, `Handle` and a real capability over a fake gateway — the
   thing every prior test of this exchange bypassed. The deadlock test on
   that harness failed against the pre-fix code with the real symptom, a
   genuine ~5s timeout (`chunkreply: no grant returned before the deadline`),
   and passes in 0.02–0.05s after — measured on this run, on Linux, against
   the fake gateway. Also falsified: no job lost across a worker restart, a
   job survives the connection being dropped under it and answers after the
   reconnect, no worker goroutine survives an idle queue, the heavy budget
   holds while light capabilities are exempt, and the replay-defence tests no
   longer depend on goroutine scheduling.

   **Gated in CI**, contrary to what this section said before: these are
   ordinary Go tests under `agent/internal/`, and the `agent` job runs
   `go test ./... -count=1` on every push — the same job §6's open-items table
   already credits with the unit, schema and budget suites. **`-race` is not
   gated**; the CI test step omits it deliberately (it would corrupt the
   idle-budget RSS thresholds measured in the same job), so that verification
   is local only.

   ~~**And the gate is currently blocked, which is worth knowing before
   trusting a green tick that is not there.**~~ **UNBLOCKED 2026-08-12.** The
   `agent` job runs `staticcheck` BEFORE `test`, and staticcheck failed on ten
   pre-existing findings (unused functions and constants in
   `capabilities/{backup,logs,patch}`, two deliberate bidi literals in a deploy
   test). Actions then skipped every later step: run 31333534459 had
   staticcheck `failure` with test, idle budget, cross-compile and packaging
   all `skipped`. All ten are now resolved — four were only "unused" at
   GOOS=linux and are consumed by darwin/windows files (suppressed with a
   directive naming the consumer, NOT deleted, which would have broken those
   builds), two were genuinely dead and are gone, one was a real dead store,
   and the two bidi literals are now `\u202e` escapes. `staticcheck ./...`
   reports zero. So the agent tests now gate in fact as well as in
   configuration. See §6.

   **What this does not prove:** the concurrency model is verified on Linux,
   against a fake gateway — not against a real gateway under real load, which
   remains the operator's deploy-validation step. And fixing this does **not**
   make `backup.run` complete — (1) above is now the single remaining reason
   a stock binary refuses every `backup.run`.

**~~Also still open: prefix-scoped READ, i.e. the RESTORE side.~~ BUILT
(prefix-scoped-READ cycle).** The original text — "SigV4 query signing cannot
express it. `backup.restore` still refuses" — is superseded, and
`agent/internal/backupstore/backupstore.go:469` says so at the code site.

A wire kind pair `backup_read_query`/`backup_read_grant` carries
repository-relative **suffixes** (never full keys), 256 per batch; the server
joins each onto the prefix it derives from the job's own `storage_ref` and
refuses traversal. `Get`/`GetRef` spend those grants; `restore.go` announces
its fetch order so grants are requested a window at a time.

**Built is not measured.** That leg **has never run against a real object
store, in any environment** — this container has no Docker daemon, so the
MinIO/Traefik stack it needs cannot come up. `docs/modules/backup/spec.md`
§6.1 item 1 states this in the same words. Read "the READ half is built" and
"the READ half works against a real store" as different claims.

### Option 4 — relay object bytes through the gateway (not started)

**No longer the restore mechanism** — prefix-scoped READ is, and it is built.
Option 4 survives as the fallback for endpoints that cannot route to object
storage at all, likely common for remote workers. Streams bytes over the
existing mTLS channel on `:8443`; needs framing, backpressure and resume.
`docs/modules/backup/spec.md` §6.1.

---

## 3. The operator's decisions, already made — do not reopen

- **Hostname `itms.toeshee.com`.** The operator supplies a real certificate for
  `:443`. It goes in `deploy/certs/` (gitignored bind mount, `fullchain.pem` +
  `privkey.pem`). The gateway on `:8443` is TCP passthrough with its own
  internal-CA certificate — **the public certificate must never go there**, as
  agents pin the internal CA at enrollment.
- **Deployment target: Ubuntu Server 24.04/26.04, Docker, via `git pull`.** Use
  Docker's own apt repo, not Ubuntu's `docker.io` — the older Engine makes
  Traefik v3 discover no containers and answer a bare 404 on everything while
  every container looks healthy.
- **Endpoints are Windows 11 and macOS.** No Linux endpoints. The `.deb` is for
  the control-plane host, not the fleet.
- **macOS needs nothing bought.** The `darwin/arm64` binary is already ad-hoc
  signed (Go's internal linker does it; `CGO_ENABLED=0` selects that linker, no
  Mac and no `codesign` required — verified on this Linux box). MDM delivers PPPC
  profiles for TCC, and MDM payloads are not quarantined so Gatekeeper does not
  assess them. Apple Developer ID and notarization stay open **for
  commercialization only** (OQ-3).
- **Windows code signing** is needed before fleet-wide rollout, not before a
  pilot with EDR exclusions on machines the operator controls.
- **Plan: deploy to Toeshee production first**, test, fix, QA — then
  commercialization.

---

## 4. The traps — every one of these was paid for

### Fixture blindness is this project's defining defect

A guard test whose fixture makes the guarded and unguarded paths produce the
**same observable value**. The assertion is then decoration and passes forever.
Found repeatedly, in every module. Sub-forms seen here:

- A mutation printing only `DID NOT RAISE` proves nothing. **Assert the state of
  the world** — a tuple like `(outcome, job count, state, job_id)`.
- A parameter constant across every fixture is untested. IPAM's
  `allocation_method` was `unknown` everywhere, so every test asserting it was
  vacuous.
- Two redundant guards mask each other. Deleting one leaves the test green;
  only deleting both fails it.
- **A mutation that silently does not apply.** A find-and-replace matching
  nothing passes, and you conclude the gate works. Always
  `assert new != old` on the edited text before running.
- A fixture that ignores the input cannot test a check on the input. The
  OpenBao CA test served a canned certificate regardless of the CSR, so a
  missing key-strength check was invisible.
- **A fixture clock that is a literal DATE is a time bomb.**
  `backup_dedup_fixtures.SEED_NOW` was `datetime(2026, 8, 9, 12, 0)` and the
  seeded job expired an hour later, so `test_the_reply_goes_down_the_devices_
  own_channel` — the one test there that routes through `handle_agent_message`
  and therefore reads the REAL clock — began failing `job_expired` at 13:00 UTC
  on 2026-08-09 and would have failed on every run after that, for ever. The
  property worth defending is ONE SHARED CLOCK, not a fixed date; anchor it to
  `datetime.now(UTC)` and keep every use relative. Fixed in `d7400f9`.
- **A suite that injects a capability's transport seam in-process cannot see a
  deadlock between the capability and the real read loop.** Every test of the
  backup dedup exchange injected `Options.QueryChunks` and answered it
  in-process, so `jobs.Runner.Handle` blocking the transport's real read
  goroutine — the defect §2 describes — was invisible to a fully green suite.
  The harness that CAN see it composes the real read loop, `Handle` and a real
  capability over a fake gateway: `agent/internal/capabilities/
  concurrency_harness_test.go`.

### RLS masks missing application predicates

A cross-org test run through `org_session` passes **even with the service's own
`org_id` predicate deleted**. Pin cross-org tests on the **admin engine**. And
build the fixture so the thing under test is the *only* discriminator — one
cross-org test passed with all three predicates removed because `Job.target_id`
already separated the orgs.

### `go test -bench` exits 0 on a regression

Measured here: a planted 64 KiB/op regression printed `65536 B/op` and exited 0.
Budget assertions must live in an ordinary `Test` calling `testing.Benchmark(…)`
and comparing.

### An after-the-fact RSS or retained-heap reading cannot see a peak

It measures what is resident *now*, and unbounded buffering is transient by
construction — freed before the handler returns. **Gate on cumulative
allocation.** A sweep found 6 of 9 capabilities could not detect the regression
they existed to detect: 3 blind gates, 1 with 5.5× slack, 2 with no gate at all.
Two real defects were hiding behind them (inventory buffering 335 MB, netsweep
5 GB).

### Prefer dimensionless gates

netsweep's fixed gate plans the same number of addresses out of a `/12` and a
`/20` — 256× apart — and requires equal cost. No picked threshold for a
regression to hide under.

### `moto` does not enforce S3 POST-policy conditions

Measured: it accepts an upload whose key violates the signed policy and answers
`204`. A "cross-prefix upload is rejected" test passes against a grant with no
conditions at all. Assert the signed policy **document**; use real MinIO for
enforcement.

### S3 presigning, measured on this stack

- **SigV4 covers the host and the full path.** A Traefik `stripPrefix` in front
  of object storage invalidates every signature (`SignatureDoesNotMatch`), and a
  prefix that is *not* the bucket name makes path-style addressing read segment 1
  as the bucket (`NoSuchBucket`). Neither is visible from the control plane —
  both only appear at the endpoint. The route works because the prefix **is** the
  bucket name, so nothing is rewritten.
- **Presigned expiry is evaluated when the request is received**, not across the
  transfer. A download that *starts* before expiry completes; widening the window
  for slow links buys nothing and lengthens a standing grant.
- **A bucket with no policy at all denies anonymous access** in MinIO — checked
  rather than assumed (`NoSuchBucketPolicy`, then 403).
- **`moto` does not enforce POST-policy conditions** (see above) but **real MinIO
  does**: 403 outside the prefix, 400 over `content-length-range`.

### Other measured facts

- **PostgreSQL refuses RLS on materialized views.**
- **NULLs do not collide in unique constraints**, and a partial index is not a
  named constraint — `ON CONFLICT ON CONSTRAINT` fails at runtime. Use
  `index_elements=` + `index_where=`.
- **On a table with a generated column**, `WITH CHECK (true)` alone is
  insufficient — the ORM's `RETURNING` is caught by the SELECT policy. Mutate
  both `USING` and `WITH CHECK`.
- **`exec.CommandContext` does not bound a shell-out.** `Wait` blocks until the
  stdout pipe closes; `sh -c "sleep 30"` under a 200 ms deadline took 30.003 s.
  Set `cmd.WaitDelay` — see `agent/internal/capabilities/inventory/exec.go`.
- **Go's `encoding/json` cannot produce `canonical_bytes`** — it HTML-escapes
  `<>&` and reformats float64. `SetEscapeHTML(false)` does not help.
- **Python's `cryptography` parses a glued PEM boundary** (`-----END
  CERTIFICATE----------BEGIN`) and returns two certs where Go returns none. A
  "does it parse" assertion cannot catch missing PEM framing.

---

## 5. What has never executed

Be precise about this in any report. The distinction between *tested* and
*compiles for that platform* matters.

- **No Windows or macOS endpoint has ever run the agent.** CI is Ubuntu-only.
  The Windows SCM dispatcher, `EvtQuery`/`EvtNext`, `NetGetJoinInformation`,
  Windows Update COM, VSS, `softwareupdate`, `log show`, MSI and `.pkg`
  installs — all compile under `GOOS=windows`/`darwin` vet and execute nowhere.
- **Every Windows and macOS testdata fixture is hand-written, not captured.**
  One is explicitly named `softwareupdate-list-RECONSTRUCTED.txt`. The apt
  fixture *is* a real capture and it exposed two shapes the parser did not
  handle — that is the evidence this matters. **Capturing real output from one
  Windows 11 box and one Mac is the highest-value follow-up.**
- **No live AI smoke test** — needs a real model provider. A scripted provider
  is not a model.
- **Deploy unverified:** the Ubuntu Docker install commands, a publicly-trusted
  certificate on the real hostname, the Keycloak console in a browser, AD/Entra
  federation, and a release-to-release `git pull` upgrade.
- **`backup.run` has still never completed end to end, on any platform** — but
  the reason has changed twice and neither original reason survives. The
  deadlock is fixed; the snapshot providers exist and are wired. What is
  missing now is simply that **nobody has run it**: a Linux root not on LVM
  still refuses, the LVM integration test skips everywhere this project has
  run (no root, no LVM tooling — a skip is not a pass), and the Windows/macOS
  providers have never executed at all. VSS in particular ships a header
  listing nine unconfirmed items, one of which (writer status never checked)
  can return `succeeded` over torn data with no error.
- **The prefix-scoped READ leg has never run against a real object store.**
  Built, unit-tested, never measured — no Docker daemon here.
- **What HAS executed, once, by hand:** the backup POST grant against real
  MinIO behind real Traefik — 403 outside the prefix, 400 over the size cap,
  204 compliant (`scripts/verify_backup_run.py`). **On Linux only, and no CI
  job runs it.** CI's `agent` job (`.github/workflows/ci.yml`) DOES run
  `agent/`'s unit, schema and idle-budget suites on every push, with a real Go
  toolchain — that job predates this branch and CLAUDE.md has said so all
  along. What has no CI is `scripts/verify_backup_run.py` itself: it is the
  live measurement against real MinIO, and it is manual and Linux-only, so a
  regression in the thing IT checks (the POST grant against a real object
  store, not the agent code) returns silently until someone runs it by hand.
  The `:443` certificate was locally issued and installed into the host trust
  store, so `backupstore`'s system-trust-roots decision is exercised, not
  proven against a publicly-issued chain.

---

## 6. Open items

| Item | Note |
|---|---|
| ~~**Prefix-scoped READ — the restore side**~~ **BUILT, NEVER MEASURED** | Was "the one storage operation still unsolved; `backup.restore` refuses". The `backup_read_query`/`backup_read_grant` pair is built on both sides and `Get`/`GetRef` spend the grants. **The leg has never run against a real object store in any environment** (no Docker daemon here), so the enforcement half is unproven exactly as the POST grant's was before it was measured. `docs/modules/backup/spec.md` §6.1 item 1 |
| **Snapshot providers exist and are wired (BK-01), `backup.run` has still never completed** | `main.go` now binds `Options.OpenSnapshot` to `backup.PlatformSnapshot`. Linux takes a real LVM snapshot for a root on an LVM logical volume — the dispatch/refusal logic is unit-tested via an injected command runner, and the one test that takes a real loopback LVM snapshot end to end (`snapshot_lvm_integration_test.go`) **skips** in every environment this project has (no root, no LVM tooling) — a skip is not a pass. A Linux root not on LVM still refuses. Windows (VSS, `snapshot_windows.go`) and macOS (APFS, `snapshot_darwin.go`) providers exist, are compile-checked for their `GOOS` (`go vet` + compile-only `go test -c`), and **have never executed** — VSS in particular ships a header listing nine specific unconfirmed items, one of which (writer status is never checked) can yield a `succeeded` result over torn data with no error at all. Do not read any of this as `backup.run` completing end to end on any platform — it has not been. See `docs/modules/backup/agent-engine.md` §4 and §11, and `docs/modules/backup/spec.md` §6.1 item 4. |
| ~~**`GOOS=darwin go vet ./...` fails module-wide, and it is a second, independent CI blocker**~~ **FIXED 2026-08-12** — the one falsifier that reaches into `readInto` moved to `budget_linux_test.go` behind `//go:build linux`, which is the guard its `runtime.GOOS` skip could never be (a skip runs after compilation; compilation was what failed). `go vet ./...` now passes on linux, windows AND darwin. Original diagnosis, kept because its second half still stands: | `internal/capabilities/health/budget_test.go:349`: `h.readInto` is undefined on darwin (`sampler_linux.go`'s `readInto` carries `//go:build linux`; `budget_test.go` has no build tag and calls it unconditionally). Pre-existing — predates this cycle, unrelated to backup — and reproducible today: `cd agent && GOOS=darwin go vet ./...`. **The identical failure also reproduces under `GOOS=windows go vet ./...`**, at the same line, for the same reason. Neither is caught by CI today, but not for the same reason going forward: `.github/workflows/ci.yml` has a `windows service code compiles (vet + test binary)` step (line ~328) that runs `GOOS=windows go vet ./...` — it is currently unreached because `staticcheck` fails first and blocks the job (see "The `agent` CI job stops before its tests", below) — so fixing staticcheck alone would surface THIS failure there next, as a second blocker, not a clean run. There is no equivalent macOS job at all: `ci.yml` only cross-compiles `darwin/amd64`/`darwin/arm64` with `go build` (which does not compile `_test.go` files) and ad-hoc-signs the `darwin/arm64` binary — neither step vets or test-compiles macOS code, so this failure would stay invisible on macOS even after staticcheck and the Windows step are both green. Flagging both halves here so the next person estimating "unblock CI" work counts two dormant breaks, not one, and so a Mac build attempt is not surprised by the pre-existing, unrelated `readInto` failure. **Amended 2026-08-13:** the "there is no equivalent macOS job at all" half is no longer true — `ci.yml`'s `agent` job now has a `macOS code compiles (vet, tests included)` step running `GOOS=darwin go vet ./...`, added by the cycle-4 review, so a darwin-tagged `_test.go` break goes red on push instead of staying invisible. |
| ~~The dedup exchange deadlocks on the agent's read goroutine~~ **FIXED** | Per-capability `Dispatcher` moved execution off the read goroutine — §2. Verified on Linux, against a fake gateway. The tests run in the `agent` CI job on every push; the `-race` verification is local only, since CI omits `-race` by design |
| Option 4 relay | Not started, and **no longer the restore mechanism** — prefix-scoped READ is. Survives as the fallback for endpoints that cannot route to object storage at all; `docs/modules/backup/spec.md` §6.1 |
| No CI for the live scripts | The `agent` CI job is configured to run `agent/`'s unit, schema and budget suites on every push. `scripts/verify_backup_run.py` — the live MinIO measurement — is manual and Linux-only and has no CI job of its own |
| ~~**The `agent` CI job stops before its tests**~~ **FIXED 2026-08-12** | `staticcheck` ran before `test` and failed on ten pre-existing findings, so Actions skipped test, idle budget, cross-compile and packaging (run 31333534459). All ten are cleared and `staticcheck ./...` reports zero; the `GOOS=windows go vet` step that would have surfaced next as a second blocker is also fixed. Verified locally with the workflow's own commands, including all six cross-compile targets (`ci.yml` iterates windows/amd64, windows/arm64, linux/amd64, linux/arm64, darwin/amd64, darwin/arm64 — "five" was miscounted here, in the spec and in the roadmap on the same day). ~~**Not yet observed green in Actions itself** — that is the one claim still owed evidence~~ **OBSERVED GREEN 2026-08-16**, run 31941209091 (PR #45): the `agent` job completed `success` with all sixteen steps green, including test, idle budget, cross-compile, the windows/macOS vet steps, shellcheck and the .deb build-install-purge. The debt is paid |
| **Alerting on error groups** | The error monitor stores groups and events; nothing pages anyone. Phase 14's notification core exists, so this is wiring plus policy (what fires, to where, with what suppression) — but the policy is a decision, not an implementation detail. `docs/08-observability.md` §3 |
| **Retag-vs-rebuild guard** | Nothing proves `docker compose up -d` REUSES a tagged image rather than rebuilding it. CI's `deploy` job checks tag interpolation only, and says so in its own comment. Needs a Docker daemon; this container has none |
| **Suppressed Celery records lose detail** | Known, contained, no decision needed |
| **Live-host upgrade, rollback, Keycloak realm restore** | The three items `docs/08-observability.md` §6 lists as unverified. Need a host, not code |
| Real AWS S3 (not MinIO) | Path-style addressing is pinned; not tested against AWS. AWS still serves path-style for existing buckets |
| Real Windows/macOS fixtures | Needs the operator's hardware |
| `package_identifier` server-side validation | Agent's `validPackageID` is authoritative; duplicating the allowlist would create drift. Operator gets a refusal, not a 422 |
| `job_log` streaming | Spec §10 wants it; `jobs.Handler` gives a capability no `Sender`. Kernel change |
| OQ-3 | Windows signing. **Self-update was decided 2026-09-23** as a signed, SHA-256-pinned `agent.update` job with local rollback and rings (`docs/modules/agent-distribution/spec-self-update.md`, not built). It no longer waits on signing. The earlier refusal ("remote code execution with a version number") was weighed against `script.run`/`app.deploy`, which already extend the same trust |
| Test ordering | **STILL TRUE, re-checked 2026-08-16.** `pytest-randomly` is neither in `pyproject.toml`'s dev extras nor installed, so the suite runs in deterministic file order and order-dependent failures do not surface. `-p no:randomly` is a silent no-op — pytest ignores an unknown `-p no:` name — so passing it (or reporting a run as "random ordering") proves nothing. One order-dependent test was found by hand in the error-monitoring cycle; there is no reason to think it was the only one |

~~`docs/06-roadmap.md:124` and `docs/modules/endpoint-health-alerting/spec.md:205`
carry the same stale "CI has three jobs, no Go toolchain" claim~~ **CORRECTED
2026-08-12.** Both now say what is true: `ci.yml` has FOUR jobs, the fourth
being `agent` with a Go 1.24 toolchain, and the reason `health.check`'s six
falsifiers remain unrun is that the capability is unwritten — not that CI
cannot see Go. The superseded text is quoted in place in both files rather than
deleted, so the correction is legible.

---

## 7. Working practice in this environment

**The container rolled back its filesystem five times in one session**, always
to the same commit, wiping the local git object store (not just refs — the
objects). Committed-and-pushed work survived all five without exception.
Uncommitted work was destroyed every time.

So:

- **Commit after every piece that goes green, however small. Push immediately.**
- Recovery is `git fetch origin <branch>` then
  `git reset --hard origin/<branch>`. **Inspect uncommitted files against the
  remote before resetting** — some will be already-committed debris, some may be
  real work.
- When several agents share the tree: `git reset` (mixed, **no pathspec**)
  before staging, then `git add` explicit paths. **Never `git add -A`,
  `git add .`, `git commit -a`, `git checkout .`, or `git stash`** — a stale
  shared index once caused a 48-file, 5,678-line accidental revert.
- Give concurrent agents **disjoint path ownership** and pre-assign migration
  revision numbers. `registry.go`-style single-list files are collision points;
  one owner, or serialise.
- To judge whether a background agent is alive, sample file mtimes **twice over
  an interval**. A single snapshot is not evidence — four agents were declared
  dead here while alive, and the duplicates that were dispatched nearly
  destroyed their work.

---

## 8. What to verify before believing any of this

This document is a summary written from a session that ended abruptly. The repo
is the authority. Start with:

```sh
git log --oneline -20
git status --porcelain
```

If a claim here disagrees with the code, the code wins — and fix this file.

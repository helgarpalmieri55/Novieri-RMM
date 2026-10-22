# Spec — Provisioning & Imaging (Phase 7)

> Full spec, expanded from `docs/modules/provisioning.md` via the brainstorming
> skill. Scope decision (approved): **control-plane-first**. Build the registry,
> the signed provisioning jobs, the answer-file rendering, and the **real** agent
> auto-enrollment handoff now; pin the site-server (PXE/WoL/LAN-cache) as an
> interface contract and defer its deployable. This mirrors how Phases 4–5 pinned
> the "site-server cache seam" and how Backup (Phase 6, BK-02) shipped a
> control-plane slice with the agent engine mocked.

---

## 1. Purpose & scope

Standardize and deploy operating systems to endpoints so new and re-provisioned
machines come up to a known baseline — golden-image deployment where the OS
supports it, modern enrollment where it does not. One workflow, **three per-OS
strategies**. The FOG/MDT/WDS-style capability, done honestly across three OSes.

**v1 delivers (real, tested in this repo):**

- A **registry** of provisionable definitions (`os_image`) and **profiles**
  (`provision_profile`) that bind an image to post-config and a target scope.
- **Answer-file rendering** — deterministic generation of per-OS unattended-setup
  artifacts (`unattend.xml` / `kickstart` / `preseed` / `cloud-init`) from a
  profile, with **vault-sealed secrets** (domain-join credentials, local-admin
  password) injected only at render time.
- **Provisioning runs** (`provision_run`) that track a target from start through
  the **real `POST /enroll/v1`** agent auto-enrollment to post-config completion.
- A signed **`provision.enroll`** agent job for post-provision configuration
  verification.
- Registry/runs **API**, **reporting**, and a **frontend** feature folder.

**v1 pins as a contract but does NOT build:**

- The **site-server deployable** (PXE/TFTP/DHCP boot serving, wake-on-LAN, LAN
  payload cache). §8 fixes the signed `provision.prepare` instruction shape and
  the site-server mTLS auth model so the future deployable slots in without
  reworking the control plane.

**Explicitly deferred (later phases):** Windows image *capture* pipeline from a
reference machine; driver management / hardware-model image variants; MDM-grade
macOS configuration profiles (APNs-driven); the site-server deployable itself.

The module registers into the kernel (job types, API routes, migration, frontend
folder) without forking core security, tenancy, or the job engine.

---

## 2. The per-OS reality (three strategies, one workflow)

"Imaging" is not one thing. Each `os_image` carries a `kind` that fixes its
strategy:

- **Windows — `image`.** A golden image (WIM/artifact) deployed via the site
  server's PXE/WinPE, driven by a rendered **`unattend.xml`** for unattended
  setup, domain join, and post-config.
- **Linux — `build`.** Not a captured image: a scripted install via PXE +
  **kickstart / preseed / cloud-init** rendered from the profile.
- **macOS — `enrollment`.** Apple Silicon **cannot be imaged**. Standardization
  is via **Automated Device Enrollment (ADE) + MDM**; the `os_image` of kind
  `enrollment` holds an ADE/MDM profile reference, and a macOS run produces an
  enrollment binding — no answer file, no PXE. MDM-grade config is a fast-follow.

The `kind` is constrained per `os_family`: `windows→image`, `linux→build`,
`macos→enrollment`. A mismatch is a validation error.

---

## 3. Data model

Additive Alembic revision **`0015`** after Phase 6's head (`0014`), with the
`_rls` helper applied to all three tables and the `provisioning.*` permission
seed + role grants (§11). All three tables get RLS `ENABLE + FORCE` +
`tenant_isolation` policy `USING (org_id = current_setting('app.current_org',
true)::uuid)`.

- **`os_image`** — `id`, `org_id`, `os_family` (windows|linux|macos), `name`,
  `kind` (image|build|enrollment), `artifact_ref` (nullable object-store ref;
  `enrollment` has none), `artifact_sha256` (nullable; integrity for
  image/build artifacts, mirrors deploy packages), `params` JSONB (per-OS:
  unattend fragments / kickstart-preseed-cloud-init source / ADE params),
  `state` (active|disabled), `created_at`. Index `(org_id, os_family)`.
- **`provision_profile`** — `id`, `org_id`, `name`, `image_id` FK, `post_config`
  JSONB (hostname pattern, domain-join settings **with secret _references_ only,
  never plaintext** — see §7, agent install/enroll options), `target_scope`
  JSONB (device-group / criteria for who this profile provisions), `enabled`,
  `created_at`. Index `(org_id, enabled)`.
- **`provision_run`** — `id`, `org_id`, `profile_id` FK, `job_id` (nullable FK
  to jobs — the post-config `provision.enroll` job), `device_id` (nullable FK —
  a bare-metal target has no `Device` row until it enrolls), `target_ref`
  (nullable text — MAC / asset tag / operator-supplied identifier for a
  pre-enrollment target), `enrollment_token_id` (nullable FK — the
  provisioning-scoped token issued for this run), `answer_ref` (nullable
  object-store ref to the rendered answer file), `state`, `log_ref` (nullable),
  `started_at`, `finished_at`, `error`, `requested_by`, `actor_type`. Index
  `(org_id, state)`.

**`provision_run.state` vocabulary (verbatim):**
`pending` → `awaiting_enrollment` → `enrolled` → `complete`, plus terminal
`failed`. (`preparing` — the site-server PXE stage — is contract-only in v1; a
run reaches `awaiting_enrollment` once its token + answer file are rendered, and
the site-server hand-off is modeled at that boundary.)

**`os_image.kind` ∈** {image, build, enrollment}. **`os_family` ∈**
{windows, linux, macos}. **`os_image.state` ∈** {active, disabled}.

---

## 4. Artifacts & the answer-file model

Two artifact classes live in object storage under the per-tenant prefix
`{org_id}/provision/…`:

- **Image/build payloads** — the golden image (Windows) or build source (Linux),
  referenced by `os_image.artifact_ref` with `artifact_sha256` integrity. The
  agent/site-server verifies the checksum before use (mirrors deploy packages).
  Large payloads are exactly what a site-server LAN cache would serve (§8).
- **Rendered answer files** — per-run `unattend.xml` / `kickstart` / `preseed` /
  `cloud-init`, produced by the renderer (§5) and stored at
  `{org_id}/provision/answers/{provision_run_id}`, referenced by
  `provision_run.answer_ref`. An answer file is **run-scoped** (it embeds a
  one-time enrollment token and injected secrets) and is **short-lived** — see
  §7 for its handling.

There is **no cross-org artifact sharing**; every object is under the org prefix.

---

## 5. The provisioning workflow

A run moves a target from a profile to a managed, standardized machine.

1. **Start (`POST /profiles/{id}/run`, `provisioning.run`).** Creates a
   `provision_run` (`pending`), computes `now` in-handler, and attributes the
   run to the calling user (`actor_type="user"`).
2. **Token issuance.** The run issues a **provisioning-scoped enrollment token**
   by reusing the existing enrollment-token subsystem
   (`app/services/enrollment.py`): time-bounded, use-limited, bound to the run's
   `org_id`. Only the token hash is stored; the plaintext secret is embedded
   into the rendered answer file (step 3) and never returned in an API response.
   Issuing the token from within a `provisioning.run`-gated flow does **not**
   require the operator to separately hold `enrollment.manage` — the run is the
   audited, RBAC-gated authority.
3. **Render (`awaiting_enrollment`).** The renderer (§below) deterministically
   produces the per-OS answer file from `profile.post_config` + `os_image.params`
   + the token, injecting vault-sealed secrets (§7). Stored at `answer_ref`. The
   run advances to `awaiting_enrollment`. For macOS (`enrollment` kind) there is
   no answer file; the run produces the ADE/MDM enrollment binding instead.
4. **Site-server hand-off (contract-only, §8).** In a full deployment the control
   plane would dispatch a signed `provision.prepare` to the site server to stage
   the payload + answer file, PXE-arm the target, and wake-on-LAN it. v1 models
   this boundary and audits the intent; it does not dispatch to a live consumer.
5. **Auto-enrollment (real).** The freshly-imaged machine boots, its bundled
   agent enrolls via the **real `POST /enroll/v1`** using the run's provisioning
   token, and a `Device`/`Agent` pair is created by the existing enrollment
   service. The run binds to the new `device_id` and advances to `enrolled`.
6. **Post-config (`provision.enroll` signed job).** A signed `provision.enroll`
   job runs on the newly-enrolled agent to verify/apply post-config (hostname,
   domain-join confirmation, agent health) and reports back. On success the run
   advances to `complete`; a failed or out-of-domain result coerces the run to
   `failed` (never crashes the handler).

**Answer-file renderer** (`render.py`): a pure, deterministic function per OS
family — `(profile, image, token, injected_secrets) → bytes`. Determinism is a
hard requirement: the same inputs produce byte-identical output, so it is fully
unit-testable without any site-server or live machine. The renderer is the only
place run secrets and the token materialize together.

---

## 6. Enrollment handoff (reuse, do not fork)

Provisioning ends with the existing kernel enrollment flow — it does **not**
invent new PKI/enrollment plumbing:

- Token issuance reuses `app/services/enrollment.py` token creation (hash-stored,
  time/use-bounded).
- The provisioned machine enrolls via `POST /enroll/v1` (`{token, csr_pem,
  device_facts}`) exactly as any agent does; `CAService.sign_csr` issues the
  cert; the enrollment service creates `Device` + `Agent`.
- The run correlates the enrollment to itself by the token it issued
  (`enrollment_token_id`), advancing `awaiting_enrollment → enrolled` and binding
  `device_id`. Post-provision the machine is inventoried immediately by the
  existing `inventory.collect` path.

---

## 7. Secrets & encryption (the security-critical crux — invariant #3)

Answer files contain the most sensitive material this module handles: **domain-
join credentials** and a **local-admin password**. These are treated with the
same discipline as Backup's repository key and the remote-control vault creds:

- `provision_profile.post_config` stores **secret _references_** (vault key ids),
  **never plaintext credentials**. The plaintext domain-join/admin secrets live
  **vault-sealed** (`app/core/vault.py` `seal_bytes`/`open_bytes`, envelope
  encryption, AAD-bound to `(org_id, profile_id)`).
- Secrets are **decrypted at point of use only** — inside the renderer, at render
  time — and injected into the answer-file bytes. They are **never** stored in
  the application DB in plaintext, **never** returned in any API response,
  **never** written to a log line, and **never** placed in audit metadata (only
  the fact of use is audited).
- The **rendered answer file** necessarily contains the injected secret + the
  one-time token. v1 does **not** require sealing it under a per-run key — MinIO
  is already the trust boundary and the object is run-scoped and short-lived —
  but the answer object is kept under the org prefix, is referenced only by
  `answer_ref`, is **never** returned through an API body, and **is deleted once
  the run reaches `complete`/`failed`** (a light cleanup, §13) so secret-bearing
  artifacts do not linger. The spec pins this boundary so a later phase can seal
  the answer object under a per-run key if the threat model tightens.
- The **enrollment token** secret follows the existing token semantics: shown
  once (embedded in the answer file), only its hash persisted.

Explicit tests assert the injected secret appears **only** in the rendered
answer bytes and is absent from the DB row, every API response, the audit log,
and logs.

---

## 8. Site-server contract (pinned, deferred)

The site server is an **authenticated extension of the control plane** (mTLS,
scoped to caching/PXE/relay — not trusted to command endpoints), per
`docs/01-architecture.md` and `docs/03-security-model.md`. v1 pins the contract
so the future deployable slots in cleanly:

- **`provision.prepare` signed instruction** — the envelope the control plane
  would send a site server: `{provision_run_id, os_family, image_ref,
  image_sha256, answer_ref, target_ref, wake_on_lan: {mac}}`, Ed25519-signed by
  the server exactly like every job (invariant #1). The site server verifies the
  signature before acting; it stages the payload + answer file, PXE-arms the
  target, and sends the WoL magic packet.
- **Site-server authentication** — mTLS to the control plane using a
  site-server-role certificate issued by the internal CA; scoped to
  caching/PXE/relay, never to job command. (Contract only; issuance flow is a
  site-server-workstream deliverable.)
- **LAN cache seam** — the site server caches image/build payloads on the LAN so
  large data stays local; agents/targets prefer the cache when present, else
  fetch `artifact_ref` from object storage. Consistent with the Phase 4–5 deploy
  cache seam.

v1 builds none of the above deployable; it fixes the shapes and audits the
`provision.prepare` intent at the run boundary.

### 8.1 Amended 2026-08-06 by the site-server module

`docs/modules/site-server/spec.md` shipped, and three things in this section are
now wrong or delivered. Recorded here rather than silently diverging.

**The site-server certificate profile does not exist and will not.** The bullet
above describes "a site-server-role certificate issued by the internal CA;
scoped to caching/PXE/relay, never to job command". There is no such profile.
A site server is a **promoted `Device`** with an ordinary device-bound agent
certificate (§0.1, §3.2). The property this bullet wanted is already what an
ordinary agent certificate gives: it authorises holding one connection bound to
one `(device_id, org_id)`, it cannot mint a job because only the server signs,
and `_load_job_for_device` discards any message about another device's job. A
second profile would need a second fingerprint→identity resolution in
`GatewayServer._resolve_agent`, the one function where identity must have
exactly one answer.

**`wake_on_lan` was silently dropped, and is now delivered.**
`RunStartRequest.wake_on_lan` was accepted by the API and never reached
`start_provision_run`, which hard-coded `wake_on_lan={}` into the prepare
envelope — an operator could request a wake and nothing happened, with no error.
It is now a typed model, threaded through `start_provision_run`, and dispatched
as a `site.wake` job through `wake_via_site_server`. Two deliberate behaviour
changes came with it: **no serving site server is a refusal before the run is
created** (starting the run and skipping the wake would reproduce the same
defect one layer down), and **`wake_on_lan` additionally requires `site.wake`**
as well as `provisioning.run`, checked at the service boundary — letting
`provisioning.run` confer LAN-wide wake for free would collapse a permission
separation the site-server spec chose deliberately.

**The discarded `provision.prepare` signature is deleted.**
`dispatch.py` computed `await signer.sign(canonical_bytes(envelope.model_dump()))`
and threw the result away: not assigned, not persisted, not published, because
no consumer existed. The discipline was real but **unguarded** — the one test
that appeared to cover it asserted only audit metadata, so deleting the line
made nothing go red. Site-server spec §8.2 wants it deleted rather than
preserved, because under §0.1 `provision.prepare` becomes an ordinary
kernel-signed job type, which is a *deletion* of a parallel signing path. §8.2
schedules that with the PXE slice, but PXE is gated on an invariant amendment
that may never be accepted, so leaving it there would keep an unguarded second
signing path in shipped code indefinitely.
`backend/tests/test_signing_boundary.py` now asserts that
`app/jobs/engine.py` is the only place in `backend/app` that produces a
signature.

**Still deferred:** `provision.prepare` becoming a registered job type remains
PXE-slice work — it has no consumer, and PXE needs `CLAUDE.md` invariant #2
amended (site-server spec §4.4, not accepted). `ProvisionPrepareEnvelope` itself
is unchanged and stays in the exported wire schema: removing a field from a
published contract is a compatibility decision, not part of this fix.

---

## 9. Post-config verification (the `provision.enroll` result)

`provision.enroll` is the agent-side post-config/verification job. Its result
records whether the machine came up to baseline: hostname set, domain-join
confirmed (where applicable), agent healthy. Result ingestion:

- Finds the `provision_run` (org-scoped by `job_id`); a no-matching-row report is
  a graceful no-op (never fails the job over server bookkeeping).
- Success → run `complete`, `finished_at` set. Failure or an out-of-domain result
  state → coerce to `failed` with an `error` (never crash/wedge the handler — the
  accumulated coercion lesson).
- A single `provisioning.result` audit event, system-attributed.

There is no "restore"; the analog of Backup's proven-restore is this post-config
verification that the standardized baseline was actually achieved.

### 9.1 Amended 2026-08-06 — the agent-side capability, as built

`agent/internal/capabilities/provision` implements `provision.enroll`. Four
decisions were made building it that this spec did not previously pin, and each
changes what an operator sees.

**(a) It verifies; it does not apply.** §5 step 6 says "verify/apply post-config"
and §9 is titled "Post-config verification"; the result fields are all past-tense
observations. The agent implements the **verify** half only. Applying would mean
spawning privileged, reboot-requiring, trust-breaking commands (`Rename-Computer`,
`Add-Computer`, `dsconfigad`) whose only authority is a params document, *after*
the answer file already did the same work correctly, on a machine that is by then
domain-joined — where a rename breaks the machine account's trust relationship
with the domain. The answer file (§4, §5 step 3) is where post-config is applied.
This job is where the control plane finds out whether it worked. Applying
post-config from the agent is **deferred**, and would need its own brief: it is a
new class of privileged endpoint action, not an extension of a verification.

**(b) "The baseline was not achieved" is a SUCCEEDED job carrying
`state: "failed"`.** `handle_provision_result` is registered as the job type's
*result handler*, and `app/jobs/registry.py` invokes those only "after a
succeeded result". A job that **fails** or is **refused** therefore never reaches
`ingest_provision_result`: the `ProvisionRun` stays on `enrolled` forever — no
terminal state, no `finished_at`, no `error` for the operator, and the
answer-file cleanup that keys off a terminal state (§7, §13) never runs, so a
secret-bearing artifact lingers in object storage. So a machine that came up
without its hostname reports a **succeeded job whose result state is `failed`**:
the job succeeded (the verification ran and produced a truthful answer) and the
*run* failed. `ingest_provision_result` coerces exactly that into
`run.state = "failed"`, which is the designed terminal path.

`Refuse` is kept for the two cases that are **not** a baseline miss: params that
are not a `ProvisionEnrollParams`, and an `os_family` that is not this machine's.
Those say "this agent will not answer this question", and leaving such a run
visibly stuck on `enrolled` is correct — a run that bound the wrong device is a
binding bug, and marking it `failed` would file it as a provisioning outcome and
close it.

**(c) `domain_joined: null` is a third answer and is used.** `null` means "this
profile asked for no domain join" **or** "this endpoint could not be asked";
`false` means "asked, and this machine is not joined". `ingest_provision_result`
writes whichever arrives straight into the hash-chained audit metadata, so the
distinction is what an auditor reads a year later. The agent never reports
`false` for a check that did not run.

**(d) Name comparison is per-label and case-insensitive.** Windows returns the
NetBIOS computer name (conventionally uppercase); a bound Mac returns an FQDN
(`ws-0142.local`, `ws-0142.corp.toeshee.local`). String equality would mark every
correctly provisioned machine on one of the two fleet platforms as failed
forever. The same rule applies to `domain_join.domain`, because
`NetGetJoinInformation` answers with the NetBIOS domain name while a profile is
conventionally written in DNS form.

**Invariant #3 on this path.** `dispatch_provision_enroll` strips every sealed
credential field from `post_config` before signing
(`_non_secret_post_config`/`_CREDENTIAL_PATHS`), so these params carry no
plaintext credential and no `{"__sealed__": …}` reference. The credential path to
the endpoint is the short-lived rendered answer file (§7), entirely upstream of
this job. The agent nevertheless treats `post_config` as hostile: it is held as
`map[string]json.RawMessage` so members it does not read are never decoded, it
reads exactly two of them (`hostname`, `domain_join.domain`), and it never logs
any of it. A test plants credentials in the params and requires them to be absent
from every log line at Debug level and from the result `error`.

**Processes spawned: none, on either fleet platform.** Windows reads directory
membership through `netapi32.dll!NetGetJoinInformation` and the hostname through
`GetComputerNameEx` (`os.Hostname`) — two library calls. macOS reads the hostname
the same way and answers directory membership with one `os.ReadDir` of
`/Library/Preferences/OpenDirectory/Configurations/Active Directory`. Neither
spawns `powershell.exe`, `wmic`, `dsconfigad` or anything else. A source-level
test walks every file in the package **behind every build tag** and fails on
`exec.Command`/`exec.CommandContext`, so the Windows and macOS files are checked
by Linux CI even though they never compile there.

**No second enrollment path.** The same walker fails on `enroll.Enroll`,
`enroll.LoadOrEnroll`, `enroll.GenerateCSR`, `enroll.Install`,
`enroll.RevertToPrevious`, `enroll.RecoverRotation`, `.ConsumeEnrollToken`, any
HTTP-request construction and any keypair generation. Only the read-only
`enroll.Load` and `enroll.ExpiryState` are permitted, and they are what answers
`agent_healthy`. An agent that could be made to enroll a second identity by a job
is an agent whose device identity is not an identity: the control plane would
hold two Device rows for one machine, and revoking one would leave the other
commanding it.

### 9.2 Idle budget for `provision.enroll` — measured, with falsifiers

§15's "dormant until a job … light post-config verification" was a design target
with no benchmark. It is now measured by
`agent/internal/capabilities/provision/budget_test.go`, as ordinary `Test`
functions calling `testing.Benchmark` (a `Benchmark` cannot gate anything —
`go test -bench` exits 0 on a regression).

| Falsifier | Observation that would break it | Measured (linux/amd64, this runner) |
| --- | --- | --- |
| 1. Anything resident with no provisioning job in flight | `AllocsPerOp > 0` on the idle path, a goroutine delta, or an extra `/proc/self/fd` entry after 50 verifications | 0 allocs/op, 0 B/op over 561,904,448 iterations; goroutine delta 0; fd delta 0 |
| 2. RSS that does not return to baseline, or that grows per job | RSS delta above 4 KiB after one verification, above 2 MiB after 10,000, or retained heap scaling across a 100× spread | −229,376 B after 1; +266,240 B after 10,000; retained heap 0 at both 100 and 10,000 (delta 0) |
| 3. A verification costing an unbounded amount | bytes/op above 8 KiB — the number a rewrite of `post_config` into `map[string]any` would move first | 1,488 B/op, 40 allocs/op over 149,986 iterations |

**Not asserted: wall-clock CPU.** Same refusal as `internal/budget`'s package
comment — a shared runner's variance exceeds the signal, and a flaky budget test
gets skipped, which reads as coverage while measuring nothing. `~0% CPU at idle`
remains a design target for this capability, approached through the
machine-independent proxies above.

**What has never executed.** `domain_windows.go` (`NetGetJoinInformation`) and
`domain_darwin.go` (the OpenDirectory configuration read) are compiled by the
`GOOS=windows`/`GOOS=darwin` vet steps and by cross-compilation, and are run by
nothing in this repository. Every branch that consumes their output — joined,
not joined, unsupported, errored — is unit-tested on Linux through the `Host`
seam. The syscalls themselves are unverified until they run on a real endpoint.

---

## 10. Reporting

Read-only aggregation (no writes, no dispatch, no audit), every query org-scoped:

- **`provision_run_status(run_id)`** — one run's state, target, timings, bound
  device, log ref.
- **`fleet_provisioning_report(org_id)`** — runs by state, success rate
  (`complete` / terminal runs, divide-by-zero → 0.0), machines provisioned in a
  window, per-profile rollups.
- **`device_provisioning(device_id)`** — the provisioning history/status tab for
  a device (which profile/image brought it up, when it enrolled).

Rates are fractions in [0,1] (frontend multiplies by 100 at display).

---

## 11. RBAC

New permissions (seeded in migration `0015`), server-side on every route and
every dispatch (invariant #6):

- `provisioning.read` — view images/profiles/runs/status.
- `provisioning.manage` — create/edit `os_image` and `provision_profile`
  (defining golden images and the profiles that embed secret references —
  admin-grade).
- `provisioning.run` — start a provisioning run against a target.

Role grants (mirror prior modules): **super-admin / org-admin** → all three;
**technician** → `provisioning.read` + `provisioning.run`; **auditor** /
**read-only** → `provisioning.read`.

---

## 12. API

Routes under `/api/v1/provisioning`, each RBAC-gated server-side; Pydantic
response models with **no secret fields** (never return domain-join/admin
credentials, token secrets, or vault refs):

- `GET/POST /images` (`read` / `manage`), `GET/PUT /images/{id}` (`read` /
  `manage`), image artifact upload (`manage`, sha256 recorded).
- `GET/POST /profiles` (`read` / `manage`), `GET/PUT /profiles/{id}` (`read` /
  `manage`).
- `POST /profiles/{id}/run` (`provisioning.run`, computes `now` in-handler,
  `actor=user`) — starts a run; response returns the run id + state, **never**
  the token secret or rendered answer bytes.
- `GET /runs` + `GET /runs/{id}` (`read`).
- `GET /devices/{id}/provisioning` (`read`) — device provisioning tab data.
- `GET /report` (`read`).

`register(app)` calls `register_job_types()` then mounts the router; `org_id` /
`actor_id` come from `AuthContext`, never the request body.

---

## 13. Worker

- **Job-type registration in all three processes** — `provision.enroll`
  registered via `register(app)` (app), `app/gateway/main.py` (gateway), and
  `register_worker_job_types()` (worker), with the un-masking test that pops the
  type from the registry, asserts `UnknownJobType`, then drives the worker
  bootstrap and asserts it resolves (the Phase-4 lesson).
- **`provision_timeout_sweep_all_orgs(now)`** — a light Celery-beat sweep
  (hourly) that marks runs stuck in `awaiting_enrollment` past a deadline as
  `failed` (a target that never came up), per org, org-scoped. Beat entry
  `provision-timeout-sweep`.
- **Answer-file cleanup** — the same sweep (or the result handler) deletes the
  run-scoped answer object once a run is terminal (`complete`/`failed`), so
  short-lived secret-bearing artifacts do not linger (§7).
- Publish-after-commit for any dispatched jobs; system attribution
  (`actor_type="system"`) for sweep-initiated actions.

---

## 14. Frontend

Feature folder `frontend/src/modules/provisioning/`:

- **ImagesPage** — register/list `os_image` (per-OS kind), `provisioning.manage`
  gated for create/edit.
- **ProfilesPage / ProfileEditor** — bind image + post-config + target scope;
  secret fields are **write-only references** (enter a credential → it is sealed
  server-side; never rendered back), `provisioning.manage` gated.
- **ProvisioningRunsPage** — runs list with state badges; a "Provision" control
  (`provisioning.run`) to start a run against a target.
- **DeviceProvisioningTab** — the device-detail provisioning tab (`read`).
- `provisioningState.ts` — state→badge maps keyed on the **real backend
  vocabulary** (`pending`/`awaiting_enrollment`/`enrolled`/`complete`/`failed`),
  no invented names. TanStack Query with query-key invalidation after mutations
  (no live-event stream). Strict TS, no `any`.

---

## 15. Security invariants & idle budget

1. **Signed jobs only** — `provision.enroll` (and the `provision.prepare`
   contract) go through the job engine + signed envelope; no unsigned path.
2. **Outbound-only mTLS** — auto-enrollment reuses the real mTLS enroll flow; the
   site-server authenticates via mTLS per §8. No inbound path to endpoints.
3. **Secrets vault-sealed** — domain-join/admin credentials sealed, decrypted
   only inside the renderer at point of use, never persisted plaintext / returned
   / logged / in audit metadata (§7). Tested.
4. **Hash-chained audit** — image/profile create-edit, run start, token issuance,
   answer-file render, `provision.prepare` intent, enroll result, post-config:
   all audited, correct user-vs-system actor.
5. **Tenancy** — RLS `ENABLE + FORCE` + explicit `org_id` predicate on every
   query over the three tables + `Device`; artifacts under the org prefix; no
   cross-org sharing.
6. **RBAC** — server-side on every route and dispatch (§11); frontend gating
   cosmetic.

**Idle budget** — `provision.enroll` is dormant until a job; the agent's role is
light post-config verification. The heavy lifting (PXE, payload serving, WoL) is
the site server's, and is deferred. No always-on capability.

---

## 16. Testing

- **Answer-file rendering** — byte-exact determinism per OS (windows/linux),
  secret injection present in the rendered bytes, and secret **absence** from the
  DB row, every API response, the audit log, and logs. Token embedded correctly.
- **Token issuance / binding** — a provisioning-scoped token is issued, hash-
  stored, bound to the run's org, and correlates the subsequent enrollment to the
  run.
- **Enrollment handoff** — a run drives the **real `POST /enroll/v1`** with its
  token; the run binds the new `device_id` and advances `awaiting_enrollment →
  enrolled`.
- **`provision.enroll` round-trip** — through `job_engine.complete` via the
  registered handler; success → `complete`, malformed/out-of-domain → `failed`
  coercion, no-matching-row graceful no-op.
- **Three-process registration** — the un-masking worker test.
- **Tenancy** — a second org's images/profiles/runs never appear in any output.
- **RBAC** — 403 for under-privileged roles on `manage`/`run` routes.
- **Timeout sweep** — a stale `awaiting_enrollment` run past the deadline →
  `failed`; answer object cleaned up on terminal state.

The site-server (`provision.prepare` consumer) and live PXE booting are **not**
tested here — they belong to the deferred site-server deployable; v1 tests the
signed envelope shape only.

---

## 17. Deploy notes

`deploy/README.md` + `.env.example` document: the per-tenant provisioning
object-storage layout (`{org_id}/provision/…`, images + run-scoped answer
files), the vault-sealed secret handling for answer files, the
`provision-timeout-sweep` beat entry (no new compose services), and the
**site-server contract seam** (PXE/WoL/LAN-cache) as a documented future
deployable — consistent with the existing deploy/backup cache-seam notes. No new
required env vars (reuses object storage, vault, redis, the enrollment CA).

---

## 18. Deferred / later phases

- The **site-server deployable**: PXE/TFTP/DHCP boot serving, wake-on-LAN, LAN
  payload cache, and its mTLS role-cert issuance flow.
- Windows **image capture** pipeline from a reference machine.
- **Driver management** / hardware-model image variants (Windows).
- **MDM-grade macOS** configuration profiles (APNs-driven) beyond ADE enrollment.
- Sealing the rendered answer object under a per-run key if the threat model
  tightens (§7).

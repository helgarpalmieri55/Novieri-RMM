# GPO MSI and subnet push — design

**Status:** approved by the operator on 2026-09-30. The design was approved
section by section, then this written spec, **including the CLAUDE.md 2a(e)
and 2a(f) amendment in §1.6**, was approved as a whole.

**Builds on:** `2026-09-28-install-link-and-windows-push-design.md`, which
covers the install link, the review gate, Windows push over WinRM and the SSH
install mode (PRs #89 and #90, merged).

**Operator decisions (2026-09-30):**

| # | Question | Answer |
|---|---|---|
| Q1 | How does a subnet push work, given 2a(f)? | The subnet fills a list the operator confirms. Only vouched-for machines are listed. 2a(f) is amended to allow this, and nothing else changes. |
| Q2 | How does an MSI-installed computer join? | Through the install link, then review, plus an **Approve all** in the platform. |
| Q3 | How is the MSI produced? | The console builds one MSI per organization and link. It is one file, with no transform. |

Two independent parts follow, each deliverable on its own. Part 2 (MSI and
Approve all) has no security-rule change and can ship first.

---

## Part 1 — Subnet push, 50 at a time

### 1.1 What the operator does

1. **Devices → Push install the agent…** has a new choice, **By subnet**,
   beside today's **Ticked machines**. Its list of subnets is exactly the
   organization's **active push authorizations** (`push_authorization.prefix`,
   native `cidr`). A subnet that isn't authorized can't be chosen, and none can
   be typed in.
2. The operator picks the subnet and the credential. The server **expands** it
   (§1.2) and shows the resulting list, one row per machine, with its readiness:
   ready, no DNS name, credential type does not match, no site server for its
   segment, not probed, and so on. Rows that aren't ready are shown and won't
   be installed.
3. The operator confirms **that list**. The confirmation carries the list's
   digest (§1.3), so it confirms those machines and not "the subnet". If the
   subnet's contents change between showing and confirming, the digest no
   longer matches, the confirmation is refused, and the list is shown again.
4. A **push run** starts. Its page shows done, failed, refused and waiting
   counts, each machine's outcome, and **Pause**, **Resume** and **Cancel**.

### 1.2 Expansion: what "the subnet" means

The machines listed are exactly the rows that satisfy **every** existing push
precondition in `preflight.py`, evaluated as it stands today:

- `devices.provenance` is in the vouched-for allow-list (`agent`, `operator`,
  `promoted`; CLAUDE.md 2a(d)). A row a sweep recorded on its own is **not
  listed**, and the page says how many such rows the subnet holds, so the
  operator can go and claim them.
- the device has no enrolled agent, and its review state is not `rejected`;
- its address is inside the chosen prefix.

These are listed **as blocked, with the reason**:

- the chosen credential is not scoped to the address (`push_credential_scope`);
- no site server serves the segment (2a(a): there is no fallback originator);
- the transport's own needs are unmet: no DNS name for WinRM, or no confirmed
  host key for SSH;
- the target is outside the credential's realm (Windows).

**Bounds:**

- **1,024 ready machines** per run (`PUSH_RUN_MAX`). A prefix wider than that
  is refused at expansion, with the advice to choose a narrower one. The
  bound counts **ready machines**, not the prefix's size: a /16 whose
  vouched-for, ready machines number 300 is fine.
- **One active run per organization.** "Active" means running or paused.
- The existing **`PUSH_BATCH_MAX = 25`** still bounds a **Ticked machines**
  push. A run is the only way past 25, and every run has a confirmed list.

### 1.3 The confirmed list is the authority

- **Tables:** new `push_run` (org_id, id, created_by, credential_id, prefix,
  list_digest, state, created_at, counters) and `push_run_target` (org_id,
  run_id, device_id, address, position, state, job_id, outcome, detail). Both
  are under forced RLS. Migration **0200**.
- **The digest:** `list_digest` is SHA-256 over the sorted `(device_id,
  address)` pairs shown. `POST /api/v1/push/runs` takes `{prefix,
  credential_id, list_digest}`. The server expands again, and refuses unless
  the digest matches, **before any row is written or any secret is touched**.
- **Fixed list:** targets are written once, at creation. No code path inserts
  into `push_run_target` for an existing run.

### 1.4 Pacing: at most 50 in flight

The pacer is `app/modules/agent_push/runs.py`, with the function
`advance_run(session, run_id)`.

**When it runs:**

- when a `push_install` job belonging to a run reaches a terminal state (the
  existing result handler calls it);
- from a Celery beat entry every 60 s, as a backstop for a missed event.

**What it does.** While the run is `running` and fewer than
**`PUSH_RUN_CONCURRENCY = 50`** of its targets are in flight, it takes the
next `waiting` target in `position` order and:

1. **re-checks the whole precondition ladder** against current data, as in
   §1.2. A target that now fails is marked `refused` with its reason and
   written to the audit log as `push.refused`. It never reaches the vault.
2. dispatches it through the **same** `_dispatch_install` path a ticked push
   uses:
   - a fresh single-use, 30-minute enrollment token;
   - the credential decrypted at that moment (`vault.checkout`, audited as
     credential access);
   - a job signed with the job-signing key, sent to the segment's site server,
     and audited as `push.dispatched` with the run's id.

Jobs are created **one at a time as capacity frees**, never all up front. A
30-minute token would expire long before position 900 in the queue was
reached, and a signed instruction queued for hours is not what an operator
confirmed.

**Concurrency:** the per-run row lock is `SELECT … FOR UPDATE` on `push_run`,
so two concurrent `advance_run` calls can't exceed 50.

### 1.4a Lockout guard: one first, then stop on sign-in failures

The operator decided this on 2026-09-30. Before it, a wrong or expired domain
password would have been presented to up to 50 machines at once, which is
enough to trip a typical Active Directory lockout policy (5–10 bad attempts)
within seconds, possibly on a Domain Admin account.

- **One first.** A run dispatches **one** target first. The pacer opens up to
  `PUSH_RUN_CONCURRENCY` only after an install in this run has reported
  successful authentication: a `push_install` result whose outcome is not
  `auth_failed`, and which got past sign-in. Until then at most one target is
  in flight. A **Resume** starts from one again.
- **Stop on failures.** When `PUSH_RUN_AUTH_FAILURE_LIMIT = 3` targets in the
  run have ended `auth_failed` since the run last started or resumed, the
  pacer moves the run to the state **`stopped_credential`**. That state is
  distinct from `paused`. From then on:
  - nothing further is dispatched;
  - in-flight installs finish;
  - it is audited as `push.run.stopped`, with the reason, recorded as the
    system acting for the run's creator;
  - the `push.run.stopped` notification event is emitted.
- **Only a person restarts it.** Resume is available on a `stopped_credential`
  run, typically after the credential has been fixed. The pacer never leaves
  `stopped_credential` on its own.

### 1.5 Controls and end states

- **Pause:** new dispatches stop, and in-flight installs finish.
- **Resume:** dispatching restarts, re-checking each target's preconditions
  as it goes.
- **Cancel:** every `waiting` target becomes `cancelled`. It is final, and a
  cancelled run can't be resumed.
- Pause, Resume and Cancel each need the push permission and are audited as
  `push.run.paused`, `push.run.resumed` and `push.run.cancelled`.
- A run whose last target is terminal becomes `completed`, and the
  `push.run.completed` notification event is emitted.
- A credential **deleted or re-scoped** mid-run is handled by the per-target
  re-check: the remaining targets are refused, one by one, with the reason.
  Nothing is dispatched with a secret the operator has since withdrawn.

### 1.6 CLAUDE.md amendment (operator approval required)

Added to 2a(e) and 2a(f) as dated notes, in the same form as the 2026-09-29
note under 2a(b). **The existing sentences are unchanged.**

> **2a(e), added 2026-09-30.** A push run the operator confirmed may be
> continued by the platform's pacer, which dispatches the confirmed targets as
> capacity allows. The pacer cannot start a run, add a target, widen a list,
> choose a credential, or resume a paused or cancelled run. Starting, pausing,
> resuming and cancelling are a person's acts, RBAC-checked and audited. The
> pacer's one act of its own is a safety stop: after repeated authentication
> failures it stops the run so a credential is not locked out, and only a
> person may restart it.
>
> **2a(f), added 2026-09-30.** A subnet may be used to *build* the list a
> person confirms, never as the thing confirmed. The act is the confirmation
> of the named machines the subnet expanded to, bound by a digest of that
> list. Only rows vouched for under (d) are ever listed. A run holds at most
> 1,024 named targets, at most 50 of which are in flight at once. A push that
> is not a run keeps the batch ceiling of 25.

**What this does not claim:** 2a's closing paragraph applies to every one of
up to 1,024 machines. An attacker who controls a confirmed address still
receives the credential. A run widens how far that property reaches per act,
from 25 to 1,024. It is bounded by (d), by prefix-scoped credentials and by
the operator seeing the list, not removed.

### 1.7 Console

- **Push install dialog:**
  - **By subnet**: subnet select, credential select, then the expanded table
    with readiness chips, a "N unclaimed sweep rows not listed" line, and
    **Start push to N machines**.
  - Starting it opens **Devices → Push runs → run page**, which shows a
    progress bar, per-state counts, a paged target table, and the
    Pause/Resume/Cancel controls.
- **Permissions:** the existing push codes. Nothing new is needed, because
  confirming a run *is* a push.

---

## Part 2 — MSI for Group Policy, and Approve all

### 2.1 What the operator does

1. In **Agent packages → Install link**, while the link is on and a stable,
   release-signed agent of **1.6.0 or later** is published for that
   architecture, click **Download MSI for Group Policy** (amd64 or arm64).
   The console shows the file's SHA-256 beside the button.
2. Copy `ITMS-Agent-<host>[@<port>]-<arch>.msi` to a share, normally under
   SYSVOL.
3. Group Policy Management → Computer Configuration → Policies → Software
   Settings → Software installation → New → Package, then **Assigned**.
4. Each computer installs the agent at its next startup, joins, and appears
   in **Devices → Waiting for review**.
5. Click **Approve all**.

### 2.2 How the server builds it

The builder is `app/modules/agent_distribution/msi.py`, with the function
`build_msi(release, console, code) -> bytes`. The download is served under
`enrollment.link.manage`.

1. It reads the published `its-agent.exe` for the architecture from the object
   store, re-hashes it against `agent_releases.sha256`, and verifies its
   release signature. The MSI never wraps bytes the release pipeline did not
   sign.
2. It writes a WiX source with fixed content:
   - `its-agent.exe` into `ProgramFiles64Folder\ITSuite`;
   - `msi-enroll.json` alongside it, holding `{"console": "https://<host>",
     "code": "<link code>"}`;
   - one deferred custom action, run as SYSTEM with no impersonation, that
     executes `"[INSTALLFOLDER]its-agent.exe" --msi-install`. **The code is
     never on that command line.**
3. **Versioning:**
   - `UpgradeCode` is a constant per organization (UUIDv5 of the org id), so a
     later MSI upgrades an earlier one rather than installing twice;
   - `ProductVersion` is the agent version.
4. It builds with `wixl` from **msitools**, which is added to
   `deploy/backend.Dockerfile`. The build runs in a temporary directory,
   bounded to 60 s, and the directory is removed afterwards. The MSI is
   **built per request and never stored**, so a regenerated code can't
   survive in a cache.
5. It audits `install_link.msi_downloaded`, recording the architecture and
   release version (never the code) and the MSI's SHA-256.

**Risk to settle first.** `wixl` implements a subset of WiX, and it is
unproven here that the subset includes a deferred, no-impersonate `ExeCommand`
custom action. The plan's first task proves it by building the MSI and reading
its `CustomAction` and `InstallExecuteSequence` tables back with `msiinfo`. If
the subset falls short, the fallback is still Linux-only and still
msitools-only: `wixl` builds the file tables and `msibuild` adds the
`CustomAction` and sequence rows. There is no Windows build host and no WiX
toolset on the server.

The MSI is **not Authenticode-signed**, because there is no certificate yet
(`agent/packaging/README.md`, "What is still unsigned"). GPO software
installation does not require a signature.

### 2.3 The agent's `--msi-install` mode

This lives in `agent/internal/install`, beside the link installer, and reuses
its steps. What it does:

- It refuses unless it runs as SYSTEM or elevated, and it asks no question:
  deploying through GPO was the operator's decision, and SYSTEM has no desktop
  to ask on.
- It reads `msi-enroll.json` from its own directory (Program Files, writable
  only by administrators). It requires `console` to be a bare https origin and
  `code` to be 32 URL-safe characters, and refuses anything else.
- It runs the link installer's sequence as it stands:
  1. secures `%ProgramData%\ITSuite`, then `agent` (`secdir.SecureOwned`,
     refusing a foreign owner on a fresh install);
  2. creates the token file with `O_EXCL`;
  3. registers the service with the console as its enrollment URL, and starts
     it.
- It then **deletes `msi-enroll.json`**. If that deletion fails, the install
  fails.
- **Already enrolled:** if `identity.json` is present, it changes nothing and
  exits 0, so re-assigning the GPO or rebooting is harmless.
- **Uninstall:** a GPO "Remove" runs the MSI uninstall, whose custom action
  runs the existing `agent.uninstall` logic locally (stop and delete the
  service, remove the data directory) before the files go.
- **Idle budget:** the idle service never calls into this mode. The existing
  import-pinning and dormant tests grow to cover it.

### 2.4 Joining is the install link's, unchanged

- The token file holds the link code, so enrollment is the link's own:
  - the allowed-networks check applies;
  - the hourly limit applies, and machines above it are answered with
    `Retry-After` and try again;
  - `review_state = pending`;
  - the `device.joined_via_link` event fires.
- **Hourly limit:** for a 300-machine rollout the operator may raise the link's
  hourly limit (maximum 500) for the day. The console says so beside the
  download.
- **Switching the link off, or regenerating it,** makes every MSI already
  handed out stop joining machines. Machines that already joined are
  unaffected.
- **Updates:** the agent updates itself as today, so a new agent release does
  not require redeploying the MSI.

### 2.5 Approve all

- **Where:** **Devices → Waiting for review → Approve all**.
- **What it shows:** a dialog with the count and the names (paged), then
  **Approve N machines**.
- **API:** `POST /api/v1/devices/review/approve-all` takes `{device_ids,
  list_digest}`. It approves only the listed ids that are still `pending`, so
  a machine that joined after the dialog opened is left pending and counted
  in the response.
- **Permission:** `devices.review`.
- **Audit:** one `device.approved` record per machine, carrying a shared
  `bulk_id`.
- **Limit:** at most 1,000 machines per call.

### 2.6 Residuals, written where the rules live

These go in `agent/packaging/README.md` and the distribution spec's §2 and §7:

- **Anyone who can read SYSVOL can take the code.** That is every domain user
  and computer, and they can take it from the MSI or its tables. The code only
  places a machine in the review queue. Revoking it is **Regenerate**.
- **The share's permissions are the only integrity control.** GPO verifies no
  digest and the MSI has no Authenticode signature. The console shows the
  SHA-256 so it can be checked once, before the file is copied.
- **The code sits on disk briefly.** `msi-enroll.json` exists in Program Files
  between file copy and custom action, and the custom action deletes it. The
  directory is *writable only by administrators*, not readable only by them:
  local users can read `msi-enroll.json` during that short window, which adds
  nothing beyond SYSVOL, where every domain user can read the same code.
- **A failed `--msi-install` is not rolled back by the MSI.** Windows
  Installer rolls back the files it copied, but not what the custom action
  had already done: a registered service or the token file may remain in the
  admin-only data directory (`%ProgramData%\ITSuite\agent`). The next attempt
  recovers (it replaces the token file and reconfigures the service it
  finds), and `%ProgramData%\ITSuite\msi.log` records why the attempt failed.

---

## Testing

- **Part 1:**
  - **Backend:**
    - expansion is exactly the §1.2 set: sweep-only rows excluded, the count
      reported, and blocked rows with each reason;
    - a digest mismatch is refused before any write or decrypt;
    - one active run per organization, and the 1,024 bound;
    - the pacer never exceeds 50 in flight under concurrent `advance_run`
      calls;
    - each dispatch re-checks preconditions, so a credential deleted mid-run
      refuses the rest;
    - Pause, Resume and Cancel are audited, Cancel is final, and the pacer
      cannot resume;
    - one first: nothing beyond one target is in flight until an
      authentication succeeds;
    - 3 `auth_failed` results stop the run as `stopped_credential`, and only
      a person's Resume restarts it, again from one;
    - no code path inserts a target into an existing run;
    - `PUSH_BATCH_MAX` still bounds ticked pushes.
  - **Frontend:** the dialog, the run page and harmony on the touched routes.
- **Part 2:**
  - **Backend:**
    - `build_msi` refuses bytes whose SHA-256 or signature does not match;
    - the built MSI (read back with `msiinfo`) holds exactly the expected
      files, the custom action and the settings file;
    - the route needs `enrollment.link.manage`, is off when the link is off,
      and is audited without the code;
    - Approve all approves only still-pending listed ids, is audited per
      device and is bounded.
  - **Agent:**
    - `--msi-install` input validation;
    - it is a no-op when already enrolled;
    - it deletes the settings file;
    - it takes the parent-first ownership path;
    - it adds nothing to the idle service (import-pinning test).
  - **CI, on windows-latest:** a new `TestMsiInstallWindows` builds the MSI with
    the real `wixl`, runs `msiexec /i … /qn` as SYSTEM through a scheduled
    task, checks the service and ACLs, checks that `msi-enroll.json` is gone
    and that the device appears pending, then runs `msiexec /x` and checks the
    removal.
  - **Not provable in CI:** real GPO assignment at computer startup. The
    operator's one-OU trial is the first run, and `deploy/WINDOWS-PUSH.md`
    gains a short GPO MSI section.

## Out of scope

- Authenticode signing of the MSI (it needs a certificate);
- Intune `.intunewin` packaging;
- a macOS `.pkg`;
- scheduling a subnet run for later: a run always starts when confirmed.

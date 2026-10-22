# Patch management, split three ways — Linux, Windows, macOS

Status: built (2026-08-27). Operator decision, 2026-08-26: split patch
management into three tabs, "because the one model the page uses does not fit
all three ecosystems". Amends `spec.md` §14 (frontend) and `view-redesign.md` §2
(panel order and severity default); **no change** to §4 (data model), §7
(approvals) or §15 (invariants). **No migration.**

---

## 0. The one thing to read if you read nothing else

**`POST /patch/apply` cannot express "install only the security updates", and it
cannot express "install everything on this host" either.** What it does is one
thing:

> install, on these machines, what is **already approved for their ring** — and
> only on the ones whose **maintenance window is open right now**.

The operator's sketch for the Linux tab was "pick host(s) → *Update security* /
*Update all* / schedule". Both of those buttons would be claims the apply path
does not honour, so neither shipped under those names. The one control that did
is called **Install approved updates**, and the two gates above are stated in the
bar, repeated in the confirmation, and accounted for in the result sentence. §3
does the full accounting.

---

## 1. Why the split is not cosmetic

`Patch.os_family` is `windows | linux | macos`, and **both** `GET /patch/updates`
and `GET /patch/device-rollup` already take an `os_family` filter. So a tab costs
no new route and no new column. What it buys is somewhere to put a disagreement
the console had been averaging over — **the three agent scanners do not mean the
same thing by "severity", and two of them barely mean anything at all**:

| Platform | Scanner | What it can actually publish |
|---|---|---|
| Windows | `winupdate.go` | `IUpdate.MsrcSeverity`, and its own comment records that "most cumulative updates for Windows 11 carry no MSRC severity at all despite containing security fixes" → `unknown` |
| Linux | `apt.go` (`aptSeverity`) | `important` **iff** the version's origin contains `security`, `unknown` otherwise. Debian and Ubuntu publish no other severity; the archive *is* the fact |
| macOS | `mac.go` (`macSeverity`) | `Recommended: YES` → `important`, everything else → `unknown`. Apple publishes **no severity at all** |

Two consequences follow immediately, and neither was expressible on one page:

**The default severity filter is different on every tab** (`defaultSeveritiesFor`).
Linux narrows to critical + important, because on apt that set **is** the
security pocket, exactly — the narrowed list is the security list. Windows and
macOS do **not** narrow: on Windows the same default hides the monthly rollup
(`view-redesign.md` §2 predicted this and could not act on it), and on macOS it
would be filtering on a field the platform does not have.

**The unit of decision is different on Linux.** apt resolves a dependency *set*,
so approving individual packages is frequently not actionable. That is the
operator's own reasoning and it is why they chose per-device apply.

---

## 2. What each tab is

| | Windows (incl. Server) | Linux | macOS |
|---|---|---|---|
| Model | WSUS: approve an update into a ring | Pick machines, install what is approved | WSUS, with Apple's limits stated |
| Panel order | Updates → machines | **Machines → updates** | Updates → machines |
| Update list | Selectable, `DecisionBar` | **Read-only evidence** | Selectable, `DecisionBar` |
| Machine list | Drill-down + `DecisionBar` | **Row checkboxes + `HostDecisionBar`** | Drill-down + `DecisionBar` |
| Severity default | all | critical + important | all |
| Commit control | Approve *n* updates (magenta) | Install approved updates (magenta) | Approve *n* updates (magenta) |

**Windows and Windows Server are one tab** because `os_family` is `windows` for
both. A separate tab would promise a filter that does not exist.

The Linux update list keeps every row and loses its checkboxes. It answers
*"what will this host receive"*, not *"which of these do I want"* — which is what
the operator's decision means on a screen.

### Reuse, not replacement

`UpdatesPanel`, `DevicesPanel`, `DecisionBar`, `ConfirmDialog` and
`selection.ts` are the 2026-08-27 rework's components, used as they are. The
Linux path adds exactly two things, both machine-shaped mirrors of what already
existed for updates:

* `HostDecisionBar.tsx` — the same always-on bar, the same "select all N
  matching", the same confirm-before-commit, over machines instead of patches;
* `resolveMatchingHostIds` in `selection.ts` — the same walk-and-resolve, over
  `GET /patch/device-rollup`, producing an explicit `device_ids` list for the
  same reason `patch_ids` is one.

---

## 3. "Update security" — what is true, and what shipped

Verified in `backend/app/modules/patch/apply.py`. `dispatch_apply` builds one
signed `patch.apply` per device out of every `(device, patch)` pair that clears
**three** gates:

1. an **un-revoked `patch_approvals` row** for `(org, patch, the device's
   resolved ring)`;
2. `device_patch_status.state` in `{approved, missing}`;
3. `window_open(policy.maintenance_window, now)` — and a device whose window is
   shut is `continue`d past entirely, producing **no job and no error**.

**There is no severity argument anywhere on that path.** So:

* *"Update security"* is **not expressible**. Not today, and not by adding a
  severity filter to `ApplyRequest` either — see §3.1.
* *"Update all"* is **not expressible**: gate 1 means only approved updates ever
  install, so a button promising "everything" would be wrong in the direction
  that matters.
* A 200 with `job_ids: []` is the **ordinary** outcome outside a maintenance
  window. A console that reported that as success is how an operator comes to
  believe a fleet was patched.

So the control is named for what it does, and `applyOutcomeMessage` reports one
job per eligible device and names the remainder:

> Install queued on 2 hosts. 3 hosts were skipped — either nothing on them is
> approved for their ring, or their maintenance window is shut right now.

Both reasons, because `ApplyResponse` carries `job_ids` and nothing else and
**cannot** distinguish them. Guessing which would invent a fact.

The confirmation also states what the agent will and will not do about a
restart: it installs, reports `reboot_required`, and **never reboots**
(`agent-capabilities.md` §4 — `deferral_hours` is parsed and acted on for
nothing, on every platform). A dialog promising a reboot would be a promise no
code in this repository keeps.

### 3.1 Why a severity filter on `/apply` was rejected rather than deferred

It needs no migration and would be ten lines, so it deserves an argument rather
than a shrug. Two, and the second is decisive:

1. **It authorises nothing.** Gate 1 still holds, so "install the approved
   security updates" is materially the same sentence as "install the approved
   updates, narrowed". The operator's mental model — *make this host secure* —
   is still not what happens.
2. **It is the model the operator rejected, one layer down.** On apt, "security"
   is a *pocket*, not a severity: a security-pocket version routinely depends on
   a version from `-updates`. A server-side severity filter over the patch list
   therefore produces a set apt may be unable to install as given — which is
   precisely *"cherry-picking single packages often isn't actionable anyway"*,
   the reason per-device apply was chosen in the first place.

**Security-first lives at approval instead**, where it already worked, and both
halves are surfaced on the tab:

* **`PatchPolicy.auto_approve_security`** — the standing setting. `ingest.py`
  step 5 clears every newly-missing critical/important patch into the **`test`**
  ring as each scan reports it. The Linux tab links to it by name.
* **The tab's own severity default** — critical + important, which on apt is the
  security pocket exactly. The update list says so
  (`APT_SECURITY_PROVENANCE`), and it is deliberately **not** said on the other
  two tabs, where the same word would be a guess.

### 3.2 What "schedule" turned into

`maintenance_window`, `reboot`, `reboot_deferral_hours` and `scan_interval_hours`
are **policy**, per device group, not a per-run choice — and `POST /apply` is
gated by the window exactly as the worker's sweep is. So the tab says what the
button is *for*:

> Approved updates install on their own at each host's next maintenance window.
> This dispatches the same install immediately — and only to hosts whose window
> is open right now.

An operator who did not know that would read an empty `job_ids` as a failure.

---

## 4. macOS: the WSUS shape, and four limits on the screen

`softwareupdate` genuinely supports scan → approve → install, so the tab is the
WSUS shape. What it does **not** support is stated in a `.notice gold` on the
tab itself, because a tab that looks like the other two and quietly cannot do
what they do is how an operator comes to believe a Mac is patched:

1. **No severity.** `--list` publishes `Recommended: YES|NO` and nothing else.
   There is no critical rating to filter on — so the Critical tile on that tab
   carries a zero-label saying it is *structurally* zero, not an all-clear.
2. **No major-version upgrades, deliberately.** `mac.go`'s `Available` runs
   `--list` **without** `--all-installers`/`--fetch-full-installer`, so a full
   macOS installer never enters the catalogue and cannot be approved by
   accident. Fleet-wide OS upgrades are declarative-MDM work
   (`docs/modules/mdm/spec.md` §17 defers DDM; §16.6 re-opens it if
   software-update enforcement turns out to be DDM-only), on a path **invariant
   1b** keeps disjoint from the signed-job engine.
3. **No deferral and no user prompt.** `agent-capabilities.md` §4: the agent
   detects a required restart and never performs one; `deferral_hours` is parsed
   and acted on for nothing. WSUS-grade "install at 02:00, defer up to 24h with
   a user prompt" does not exist here — on any platform.
4. **Apple Silicon OS updates need a volume owner's credentials**
   (`--user`/`--stdinpass`), which invariant 3 keeps in the vault with no
   agent-side equivalent. Those installs fail with Apple's own authorisation
   message rather than silently.

**A fifth is true and is not on the screen**, deliberately:
`agent-capabilities.md` §6 records that `softwareupdate` has never executed
anywhere and that
`testdata/softwareupdate-list-RECONSTRUCTED.txt` was written by hand from the
documented format. If Apple's real output differs, the tests stay green and
every Mac reports as fully patched. That is a fact about this build's *test
coverage*, not about what the product can do for a correctly-reporting fleet,
and it belongs in the release note and the capability doc rather than in
permanent chrome an operator cannot act on. **It is the single highest-value
thing to close on this module** — one captured `--list` from one real Mac.

---

## 5. Where the tab lives, and which house pattern it follows

**`?os=windows|linux|macos`, a search param, with `Tabs` from
`shell/components` and a `role="tabpanel"` the strip names.** That is the
devices page's `?state=` precedent, not `IpamNav`'s, and the choice is not
arbitrary:

* `IpamNav` makes each surface a **route** (`/ipam/drift`). Right when the
  surfaces are different pages with different data.
* The devices page makes its strip a **search param** over one dataset. Right
  when the tabs are one predicate over shared routes.

All three patch tabs are the same two routes with one predicate changed, under
one page head, above one shared record. Three routes would be three copies of
`FleetPatchPage`. And `?os=` already existed in this page's URL vocabulary — it
was a select in the update list's filter bar; the split *promotes* it to primary
navigation and deletes the select, so there is no reachable state where the
Windows tab shows a list filtered to Linux.

A reload or a pasted link lands on the tab it names, and Back walks the tabs.
Windows is the default and it is a **stable** one — "the tab with the most
outstanding work" would move under the operator between visits and could not be
linked to. The cost is paid off by the **count on every tab**, read from
`total_unfiltered` off the same route the tab's own table is paged from: land on
Windows, see "Linux 41" beside it, and you have been told where the work is
before reading a row.

**Switching tabs clears `severity`, `q`, both offsets and both selections.** Not
tidiness — the severity default is platform-specific, the offsets are positions
in a different list, and a selection is a set of ids belonging to the platform it
was made on.

---

## 6. The counts still cannot disagree with the tables

The property `view-redesign.md` established survives the scoping, and got
slightly stronger:

* Tiles 1–3 are `total_unfiltered` and two buckets of `severity_counts` off the
  **same response** the update table is paged from, and
  `sum(severity_counts) == total_unfiltered` by construction on the server — so
  the severity tiles are components of the total beside them and can never
  exceed it.
* Tile 4 is the device rollup's `total`, and its link is the list it came from.
* The tab-strip counts are `total_unfiltered` from the same route.

**Two numbers had to leave, both because they are org-wide and could not be
scoped**:

* **"Devices with critical exposure"** read `FleetReport.critical_device_count`,
  which `reporting.fleet_report` computes with no `os_family` predicate
  anywhere in it. On a platform tab it described a different set of machines
  from the list it linked to — the exact tile/table disagreement this module has
  now shipped twice. `severity_counts.critical` is the scoped fact that
  replaced it.
* **The device panel's denominator** was `devices_total` from
  `fleet_scan_coverage`, also org-wide. "3 of 286" under a Windows-only table
  measures Windows machines against a fleet that is mostly not Windows. It now
  comes from `GET /devices?os_family=…` (`state_counts.all - state_counts.assets`
  — enrolment, since a machine with no agent cannot be scanned at all), and is
  **omitted rather than substituted** when the operator lacks `devices.read`.

---

## 7. Three empty tabs, three different sentences

`CompliancePanels` painted a row of 0% bars on a fleet nothing had ever scanned;
the dashboard put nine confident zeros on an unmeasured page. The
discrimination is now a pure function with its own tests (`tabEmptyReason`), and
the component only chooses words:

| Reason | Test | What the tab says |
|---|---|---|
| `unenrolled` | this platform's managed count is 0 | *No Windows machine is enrolled* — "what is empty here is the estate, not the update list". Way out: Add devices |
| `unscanned` | machines exist, `devices_scanned == 0` | *No patch scan has completed yet* — "empty because nobody has looked, not because nothing is outstanding" |
| `compliant` | counted **and** measured | *Every Windows machine is up to date* — "the one empty state on this page that is good news", with the last scan time |
| `uncounted` | no `devices.read` | *Nothing outstanding on any Windows machine that has reported* — and it says plainly it cannot tell an up-to-date fleet from an empty one, "before reading this as an all-clear" |

Enrolment is checked **first**: `devices_scanned` is org-wide, so a zero there on
a tab with no machines of its own would name the wrong cause.

**The tiles stay even when every one of them is zero.** A zero is a
measurement, and "0 outstanding" vs "never measured" are the same pixel unless
the tile says which — so the row keeps its `zeroLabel`s and its "Never scanned"
feet, and the empty panel replaces the two working lists below it. Number
above, reason below.

---

## 8. The kernel change, and it is one line of behaviour

`FormActions` hard-coded `Button`'s default variant, which **is** the commit
magenta — so every form in the product had to be the loudest thing on its
screen, and `/patch` was already breaking DESIGN.md's Two Voices Rule before
this cycle: the page head's **Scan** button is a `Form`/`FormActions` pair, so a
fleet *scan* — a read, which changes nothing — was painted in the hue reserved
for the one control on a screen that commits.

`FormActions` now takes `variant`, defaulting to `default` so no existing caller
moves, and Scan passes `outline`. Fixed in the kernel rather than around it
(CLAUDE.md build discipline §3): the workaround every call site reaches for is
dropping back to a plain `<Button>`, which takes the announced error and the
no-optimistic-success rule with it.

**Magenta, per tab, after the change:** at rest, none. With a selection standing,
exactly one — *Approve n updates* on Windows/macOS, *Install approved updates* on
Linux. A second is reachable only by holding two selections in two different
scopes at once (a host selection plus a drill-down update selection), which is a
deliberate, transient state and the reason `DecisionBar` takes a `scopeName`.

---

## 9. Verified how

* **Browser (Chromium, `/__preview`)** — `scripts/a11y-browser.mjs`, exit `0`,
  **0 findings**: axe (wcag2a/2aa/21a/21aa/22aa) clean at 1440, 1024, 768, 390
  and **320**px; every text run's composited contrast sampled; no overflow
  container; the confirm dialog's focus trap and focus return measured. The
  `--falsify` self-check ran first and reported all three planted defects.
* **The harness renders the tabs.** `/__preview?os=linux` drives the embedded
  `FleetPatchPage` onto the Linux tab, and `PatchWorkingSurfaces` mounts the
  states a resting page cannot reach: a host selection standing (the magenta
  install, the count, the clear), the drill-down expanded, and `DecisionBar`
  committed. Fixtures are split by platform and each is **honest to its own
  scanner** — no `critical` on macOS, no `moderate` on apt.
* **`npx vitest run`** — 3005 passed / 128 files. **`npx tsc --noEmit`** and
  **`npx vite build`** clean.

## 10. Found, not fixed

**`StatsGrid` sets `grid-template-columns` inline, which defeats every
responsive rule in `shell/components.css`.** Lines 685, 696 and 705 relax
`.stats-grid` to 3, then 2, then 1 column; an inline style beats all of them, so
every page in the product using `<StatsGrid columns={4}>` renders four columns
down to 320px. This is why a longer tile label clipped inside a 96px tile at
390px, and shortening the label is a workaround, not the fix. It lives in
`frontend/src/shell/components.tsx` — the kernel, and outside this cycle's
scope. The fix is to pass the count as a class or a custom property so the media
queries can win.

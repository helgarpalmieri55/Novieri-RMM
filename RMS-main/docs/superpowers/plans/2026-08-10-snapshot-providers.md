# Snapshot Providers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `backup.Options.OpenSnapshot` for Windows (VSS), macOS (APFS) and Linux (LVM), so `backup.run` stops refusing before it starts.

**Architecture:** Platform-independent *logic* lives in untagged files (`snapshot.go`, `snapshot_lvm.go`, `snapshot_apfs.go`) that take an injected command `runner`, so LVM and APFS decision-making is unit-tested on any machine. Only the thin `PlatformSnapshot` dispatch is build-tagged (`snapshot_linux.go`, `snapshot_darwin.go`, `snapshot_windows.go`, `snapshot_other.go`). VSS is the exception: it needs `x/sys/windows`, so both its logic and dispatch are Windows-tagged and verified compile-only.

**Tech Stack:** Go 1.24, standard library plus `golang.org/x/sys/windows` (already required at `agent/go.mod:7`). No new dependency.

**Spec:** `docs/superpowers/specs/2026-08-10-snapshot-providers-design.md` (commit `894c28f`). Where this plan and the spec disagree, the spec wins — raise the conflict rather than silently following either.

## Global Constraints

- **No new dependency.** Standard library plus `golang.org/x/sys/windows`.
- **No fallback to the live filesystem, ever.** `defaultOpenSnapshot` refuses because a live-filesystem backup "would produce a backup point that restores torn files and reports success". No provider may read live data when its snapshot mechanism is unavailable. Refuse instead.
- **A snapshot kind that does not match the platform is REFUSED**, naming both what was asked for and what this endpoint provides. The kind arrives inside a signed job; it is honoured, not reinterpreted.
- **The orphan tag is the literal string `toeshee-snap-`**, followed by the job id. Agent-created mountpoints live under `/var/lib/toeshee-agent/snap/`. Exact strings, not conventions.
- **The entry-count rule is exact:** refuse when the live root's immediate-child count is greater than zero AND the snapshot root's is zero. Nothing else refuses.
- **`Release()` continues past a failure** and reports the first error. An early return leaks the remaining volumes.
- **Refusals use `jobs.Refuse(...)`**, which produces `state=refused` — a standing incapacity, not a transient failure — and the reason lands in the server's hash-chained audit log. Never format a job-supplied string into a refusal; use `clean(...)` as `defaultOpenSnapshot` does.
- **Windows and macOS provider files carry a "this has never executed" header** in the shape of `agent/internal/capabilities/patch/com_windows.go`'s, naming what verification they do have and what would falsify them.
- **Do not edit `.github/workflows/`.** The agent CI job is separately blocked behind ten pre-existing staticcheck findings (`HANDOFF.md` §6); unblocking it is its own piece of work.
- **Never `git add -A`, `git add .`, `git commit -a`, `git checkout .`, or `git stash`.** Bare `git reset` first, then `git add` explicit paths. Use `git worktree` if you need a clean tree.
- Push with `git push -u origin claude/handoff-spec-review-f45idd`.

---

### Task 1: The shared core

**Files:**
- Create: `agent/internal/capabilities/backup/snapshot.go`
- Create: `agent/internal/capabilities/backup/snapshot_other.go`
- Test: `agent/internal/capabilities/backup/snapshot_test.go`

**Interfaces:**
- Consumes: the existing `Snapshot` interface (`backup.go:172-175`) — `Translate(original string) string` and `Release() error`.
- Produces, for every later task:
  - `type snapRunner func(ctx context.Context, env []string, name string, args ...string) snapResult`
  - `type snapResult struct { Stdout, Stderr string; ExitCode int; Err error }`
  - `func execSnapRunner(ctx context.Context, extraEnv []string, name string, args ...string) snapResult`
  - `const snapTagPrefix = "toeshee-snap-"` and `const snapMountRoot = "/var/lib/toeshee-agent/snap"`
  - `func requireKind(got, want string) error`
  - `type volSnapshot struct { origin, translated string; teardown func() error }`
  - `func newMultiSnapshot(vols []volSnapshot) Snapshot`
  - `func verifyPresents(roots []string, s Snapshot) error`

**Why the runner is duplicated rather than imported:** `patch`'s `runner` and `commandResult` are unexported in package `patch` and cannot be imported. This tree duplicates such helpers deliberately — see `internal/capabilities/netsweep/netsweep_test.go:1240`, "It is duplicated rather than shared."

- [ ] **Step 1: Write the failing tests**

Create `agent/internal/capabilities/backup/snapshot_test.go` in `package backup`:

```go
func TestRequireKindRefusesAMismatchNamingBothSides(t *testing.T) {
	err := requireKind(snapshotLVM, snapshotVSS)
	if err == nil {
		t.Fatal("requireKind(lvm, vss) = nil, want a refusal")
	}
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("err is %T, want *jobs.Refusal: a wrong kind is a standing incapacity", err)
	}
	for _, want := range []string{snapshotLVM, snapshotVSS} {
		if !strings.Contains(refusal.Reason, want) {
			t.Errorf("reason %q does not name %q; an operator cannot act on it", refusal.Reason, want)
		}
	}
	if err := requireKind(snapshotVSS, snapshotVSS); err != nil {
		t.Fatalf("requireKind(vss, vss) = %v, want nil", err)
	}
}

func TestTranslateResolvesThroughTheLongestMatchingPrefix(t *testing.T) {
	s := newMultiSnapshot([]volSnapshot{
		{origin: "/data", translated: "/snap/a"},
		{origin: "/data/inner", translated: "/snap/b"},
	})
	for _, tc := range []struct{ in, want string }{
		{"/data/x", "/snap/a/x"},
		{"/data/inner/y", "/snap/b/y"},
		{"/elsewhere/z", "/elsewhere/z"},
	} {
		if got := s.Translate(tc.in); got != tc.want {
			t.Errorf("Translate(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestReleaseContinuesPastAFailureAndReportsTheFirst(t *testing.T) {
	var released []string
	first := errors.New("first teardown failed")
	s := newMultiSnapshot([]volSnapshot{
		{origin: "/a", translated: "/s/a", teardown: func() error { released = append(released, "a"); return first }},
		{origin: "/b", translated: "/s/b", teardown: func() error { released = append(released, "b"); return nil }},
	})

	err := s.Release()

	if got := (struct {
		err      error
		released int
	}{err, len(released)}); !errors.Is(got.err, first) || got.released != 2 {
		t.Fatalf("got %+v, want the first error and 2 teardowns run; an early return leaks the rest", got)
	}
}

func TestVerifyPresentsRefusesOnlyWhenLiveIsPopulatedAndSnapshotIsEmpty(t *testing.T) {
	live := t.TempDir()
	if err := os.WriteFile(filepath.Join(live, "f"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	emptySnap := t.TempDir()
	fullSnap := t.TempDir()
	if err := os.WriteFile(filepath.Join(fullSnap, "f"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	emptyLive := t.TempDir()

	if err := verifyPresents([]string{live}, newMultiSnapshot([]volSnapshot{{origin: live, translated: emptySnap}})); err == nil {
		t.Error("populated live + empty snapshot = nil, want a refusal: this is the TCC-denial signature")
	}
	if err := verifyPresents([]string{live}, newMultiSnapshot([]volSnapshot{{origin: live, translated: fullSnap}})); err != nil {
		t.Errorf("populated live + populated snapshot = %v, want nil", err)
	}
	if err := verifyPresents([]string{emptyLive}, newMultiSnapshot([]volSnapshot{{origin: emptyLive, translated: emptySnap}})); err != nil {
		t.Errorf("empty live + empty snapshot = %v, want nil: a genuinely empty root is fine", err)
	}
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agent && go test ./internal/capabilities/backup/ -run 'TestRequireKind|TestTranslate|TestReleaseContinues|TestVerifyPresents' -count=1`
Expected: FAIL — `undefined: requireKind`, `undefined: newMultiSnapshot`, `undefined: verifyPresents`.

- [ ] **Step 3: Write `snapshot.go`**

```go
package backup

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// snapTagPrefix is the literal marker on every snapshot this agent creates.
//
// It is an EXACT string, not a convention: the orphan sweep removes anything
// carrying it, so a sweep matching a wider pattern than it creates would delete
// a volume the agent does not own.
const snapTagPrefix = "toeshee-snap-"

// snapMountRoot is where the agent mounts snapshots it takes. The sweep
// reclaims mountpoints under this directory and nowhere else.
const snapMountRoot = "/var/lib/toeshee-agent/snap"

type snapResult struct {
	Stdout   string
	Stderr   string
	ExitCode int
	Err      error
}

func (r snapResult) ok() bool { return r.Err == nil }

// snapRunner is the injected seam the shell-out providers take, so LVM's and
// APFS's decision-making is unit-tested on a machine that has neither.
//
// patch's `runner` is the same shape but unexported in its own package. It is
// duplicated here rather than shared, per the convention stated at
// internal/capabilities/netsweep/netsweep_test.go:1240.
type snapRunner func(ctx context.Context, env []string, name string, args ...string) snapResult

func execSnapRunner(ctx context.Context, extraEnv []string, name string, args ...string) snapResult {
	path, err := exec.LookPath(name)
	if err != nil {
		return snapResult{ExitCode: -1, Err: err}
	}
	cmd := exec.CommandContext(ctx, path, args...)
	if len(extraEnv) > 0 {
		cmd.Env = append(os.Environ(), extraEnv...)
	}
	var out, errb strings.Builder
	cmd.Stdout, cmd.Stderr = &out, &errb
	runErr := cmd.Run()
	res := snapResult{Stdout: out.String(), Stderr: errb.String(), Err: runErr}
	var ee *exec.ExitError
	if errors.As(runErr, &ee) {
		res.ExitCode = ee.ExitCode()
	}
	return res
}

// requireKind refuses a snapshot kind that does not match this platform.
//
// The kind arrives inside a signed job. Silently substituting the locally
// correct mechanism would discard a signed instruction; refusing puts the
// server's misconfiguration in the job record where an operator sees it.
func requireKind(got, want string) error {
	if got == want {
		return nil
	}
	return jobs.Refuse("backup.run: this endpoint provides the %s snapshot mechanism, "+
		"but the job asked for %s", want, clean(got))
}

// volSnapshot is one snapshotted volume: where it came from, where its contents
// now appear, and how to tear it down.
type volSnapshot struct {
	origin     string
	translated string
	teardown   func() error
}

type multiSnapshot struct{ vols []volSnapshot }

// newMultiSnapshot orders volumes longest-origin-first so Translate resolves
// through the most specific prefix: /data/inner must win over /data.
func newMultiSnapshot(vols []volSnapshot) Snapshot {
	ordered := append([]volSnapshot(nil), vols...)
	sort.Slice(ordered, func(i, j int) bool { return len(ordered[i].origin) > len(ordered[j].origin) })
	return &multiSnapshot{vols: ordered}
}

func (m *multiSnapshot) Translate(original string) string {
	for _, v := range m.vols {
		if original == v.origin {
			return v.translated
		}
		if strings.HasPrefix(original, v.origin+string(os.PathSeparator)) {
			return filepath.Join(v.translated, strings.TrimPrefix(original, v.origin))
		}
	}
	return original
}

// Release tears down every volume and reports the FIRST failure.
//
// It does not stop at the first error: returning early would leave the
// remaining snapshots mounted and, on LVM, consuming copy-on-write space until
// they are invalidated.
func (m *multiSnapshot) Release() error {
	var first error
	for _, v := range m.vols {
		if v.teardown == nil {
			continue
		}
		if err := v.teardown(); err != nil && first == nil {
			first = err
		}
	}
	return first
}

// verifyPresents refuses a snapshot that does not actually present the data.
//
// The rule is exact: refuse when the live root has children and the snapshot
// root has none. Nothing else refuses -- a smaller count is normal on a running
// system, and a genuinely empty root is fine.
//
// This is what catches a macOS Full Disk Access denial, where mount_apfs
// succeeds and the tree reads empty, so the backup would complete, upload
// almost nothing, and REPORT SUCCESS.
func verifyPresents(roots []string, s Snapshot) error {
	for _, root := range roots {
		liveN, err := countChildren(root)
		if err != nil {
			return jobs.Refuse("backup.run: cannot read the root %s to verify the snapshot", clean(root))
		}
		if liveN == 0 {
			continue
		}
		snapN, err := countChildren(s.Translate(root))
		if err != nil {
			return jobs.Refuse("backup.run: the snapshot does not present the root %s", clean(root))
		}
		if snapN == 0 {
			return jobs.Refuse("backup.run: the snapshot presents no entries under %s, but the live "+
				"filesystem has %d. Backing up from it would report success having stored nothing. "+
				"On macOS this is what a missing Full Disk Access grant looks like", clean(root), liveN)
		}
	}
	return nil
}

func countChildren(dir string) (int, error) {
	f, err := os.Open(dir)
	if err != nil {
		return 0, err
	}
	defer f.Close() //nolint:errcheck
	names, err := f.Readdirnames(-1)
	if err != nil {
		return 0, err
	}
	return len(names), nil
}

// snapMountPath is where one volume's snapshot is mounted for a given job.
func snapMountPath(jobID string, n int) string {
	return filepath.Join(snapMountRoot, fmt.Sprintf("%s%s-%d", snapTagPrefix, jobID, n))
}
```

- [ ] **Step 4: Write `snapshot_other.go`**

```go
//go:build !windows && !darwin && !linux

package backup

import (
	"context"
	"runtime"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// PlatformSnapshot refuses on a platform with no snapshot provider.
//
// It exists so main.go can wire Options.OpenSnapshot unconditionally: a build
// for an unsupported GOOS refuses at the seam rather than failing to compile.
func PlatformSnapshot(_ context.Context, kind string, _ []string) (Snapshot, error) {
	return nil, jobs.Refuse("backup.run: this build targets %s, which has no volume snapshot "+
		"provider. Backing up the live filesystem instead would produce a backup point that "+
		"restores torn files and reports success", runtime.GOOS)
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd agent && go test ./internal/capabilities/backup/ -race -count=1`
Expected: PASS, whole package.

- [ ] **Step 6: Falsify the kind check**

Spec §4 requires this guard be shown failing. In `requireKind`, replace the body with `return nil`. Confirm the edit applied by re-reading the file, then run `TestRequireKindRefusesAMismatchNamingBothSides`.
Expected: **FAIL** — `requireKind(lvm, vss) = nil, want a refusal`. Report the output, revert, and confirm `git diff` on `snapshot.go` is empty.

- [ ] **Step 7: Falsify `Release`'s continuation**

Spec §4 requires this too. In `multiSnapshot.Release`, change the loop body to `return err` on the first failure instead of recording it. Confirm the edit applied, then run `TestReleaseContinuesPastAFailureAndReportsTheFirst`.
Expected: **FAIL** — only 1 teardown ran, not 2. That is the leak this guard exists to prevent: on LVM the un-released volume keeps consuming copy-on-write space until it is silently invalidated. Report the output, revert, and confirm the diff is empty.

- [ ] **Step 8: Commit**

```bash
git reset
git add agent/internal/capabilities/backup/snapshot.go agent/internal/capabilities/backup/snapshot_other.go agent/internal/capabilities/backup/snapshot_test.go
git commit -m "feat(agent/backup): the shared core for snapshot providers

The injected runner seam, the exact toeshee-snap- tag, kind-mismatch refusal,
a multi-volume Snapshot whose Release continues past a failure, and the
entry-count check that catches a snapshot presenting no data."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 2: LVM logic

**Files:**
- Create: `agent/internal/capabilities/backup/snapshot_lvm.go` (NO build tag — this is what makes it testable everywhere)
- Test: `agent/internal/capabilities/backup/snapshot_lvm_test.go`

**Interfaces:**
- Consumes: `snapRunner`, `snapResult`, `snapTagPrefix`, `snapMountRoot`, `requireKind`, `volSnapshot`, `newMultiSnapshot`, `verifyPresents`, `snapMountPath` (Task 1).
- Produces: `func openLVM(ctx context.Context, run snapRunner, jobID string, roots []string) (Snapshot, error)` and `func sweepLVMOrphans(ctx context.Context, run snapRunner) error`, both used by Task 3's dispatch.

**No build tag is deliberate.** The logic only shells out, so with an injected runner it is exercised on any machine. Only the dispatch that selects it needs `//go:build linux`.

- [ ] **Step 1: Write the failing tests**

Create `agent/internal/capabilities/backup/snapshot_lvm_test.go` in `package backup`. Use a fake runner recording argv:

```go
type fakeRun struct {
	calls [][]string
	reply func(name string, args []string) snapResult
}

func (f *fakeRun) run(_ context.Context, _ []string, name string, args ...string) snapResult {
	f.calls = append(f.calls, append([]string{name}, args...))
	if f.reply != nil {
		return f.reply(name, args)
	}
	return snapResult{}
}

func TestOpenLVMRefusesARootThatIsNotOnALogicalVolume(t *testing.T) {
	f := &fakeRun{reply: func(name string, args []string) snapResult {
		switch name {
		case "findmnt":
			return snapResult{Stdout: "/dev/sda1\n"}
		case "lvs":
			return snapResult{ExitCode: 5, Err: errors.New("not an LV")}
		}
		return snapResult{}
	}}

	_, err := openLVM(context.Background(), f.run, "job1", []string{"/data"})

	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("err is %T, want *jobs.Refusal", err)
	}
	for _, want := range []string{"/data", "/dev/sda1"} {
		if !strings.Contains(refusal.Reason, want) {
			t.Errorf("reason %q does not name %q", refusal.Reason, want)
		}
	}
	for _, c := range f.calls {
		if c[0] == "lvcreate" {
			t.Fatal("lvcreate was called for a non-LV root; it must refuse before creating anything")
		}
	}
}

func TestOpenLVMNamesTheSnapshotWithTheAgentTag(t *testing.T) {
	f := &fakeRun{reply: func(name string, args []string) snapResult {
		switch name {
		case "findmnt":
			return snapResult{Stdout: "/dev/vg0/root\n"}
		case "lvs":
			return snapResult{Stdout: "  vg0 root\n"}
		}
		return snapResult{}
	}}

	if _, err := openLVM(context.Background(), f.run, "job42", []string{"/data"}); err != nil {
		t.Fatalf("openLVM = %v, want nil", err)
	}

	var created string
	for _, c := range f.calls {
		if c[0] == "lvcreate" {
			for i, a := range c {
				if a == "-n" && i+1 < len(c) {
					created = c[i+1]
				}
			}
		}
	}
	if !strings.HasPrefix(created, snapTagPrefix) || !strings.Contains(created, "job42") {
		t.Fatalf("lvcreate -n %q, want a name prefixed %q and carrying the job id", created, snapTagPrefix)
	}
}

func TestSweepLVMOrphansRemovesOnlyTaggedVolumes(t *testing.T) {
	f := &fakeRun{reply: func(name string, args []string) snapResult {
		if name == "lvs" {
			return snapResult{Stdout: "  vg0 " + snapTagPrefix + "old\n  vg0 home\n  vg0 " + snapTagPrefix + "older\n"}
		}
		return snapResult{}
	}}

	if err := sweepLVMOrphans(context.Background(), f.run); err != nil {
		t.Fatalf("sweepLVMOrphans = %v, want nil", err)
	}

	var removed []string
	for _, c := range f.calls {
		if c[0] == "lvremove" {
			removed = append(removed, c[len(c)-1])
		}
	}
	sort.Strings(removed)
	want := []string{"vg0/" + snapTagPrefix + "old", "vg0/" + snapTagPrefix + "older"}
	if !reflect.DeepEqual(removed, want) {
		t.Fatalf("removed %v, want %v: the sweep must never touch a volume the agent did not create", removed, want)
	}
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agent && go test ./internal/capabilities/backup/ -run 'TestOpenLVM|TestSweepLVM' -count=1`
Expected: FAIL — `undefined: openLVM`, `undefined: sweepLVMOrphans`.

- [ ] **Step 3: Write `snapshot_lvm.go`**

Implement:

- `sweepLVMOrphans` — `lvs --noheadings -o vg_name,lv_name`, parse two fields per line, and for every LV whose name starts with `snapTagPrefix`, `lvremove -f <vg>/<lv>`. Collect errors; return the first. Never remove a name without the prefix.
- `openLVM` — for each root: `findmnt -n -o SOURCE --target <root>` for the device; `lvs --noheadings -o vg_name,lv_name <device>` to confirm it is an LV and get vg/lv (non-zero exit → `jobs.Refuse` naming the root and the device, before any `lvcreate`); `lvcreate -s -n <snapTagPrefix><jobID>-<n> -l 10%ORIGIN /dev/<vg>/<lv>`; `mkdir -p` the mountpoint from `snapMountPath`; `mount -o ro /dev/<vg>/<snapname> <mountpoint>`. Build a `volSnapshot` whose `teardown` unmounts then `lvremove -f`s, in that order. Call `sweepLVMOrphans` before the first `lvcreate`. After building the `Snapshot`, call `verifyPresents(roots, s)` and, if it refuses, release before returning the refusal.

**`10%ORIGIN` is the spec's reasoned default, not a measured one** (spec §3.3). Put that in the code comment, not just here.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agent && go test ./internal/capabilities/backup/ -race -count=1`
Expected: PASS.

- [ ] **Step 5: Falsify the sweep filter**

Widen the prefix test in `sweepLVMOrphans` so it removes every LV rather than only tagged ones. Confirm the edit applied by re-reading the file, then run `TestSweepLVMOrphansRemovesOnlyTaggedVolumes`.
Expected: **FAIL** — `removed` includes `vg0/home`. Report the output, revert, and confirm `git diff` on the file is empty.

- [ ] **Step 6: Commit**

```bash
git reset
git add agent/internal/capabilities/backup/snapshot_lvm.go agent/internal/capabilities/backup/snapshot_lvm_test.go
git commit -m "feat(agent/backup): LVM snapshot logic, testable without LVM

No build tag: the logic only shells out, so with the injected runner it is
exercised on any machine. Falsified: widening the sweep's prefix makes it
remove an LV the agent never created."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 3: Linux dispatch and the loopback integration test

**Files:**
- Create: `agent/internal/capabilities/backup/snapshot_linux.go`
- Create: `agent/internal/capabilities/backup/snapshot_lvm_integration_test.go`

**Interfaces:**
- Consumes: `openLVM`, `sweepLVMOrphans` (Task 2); `requireKind` (Task 1).
- Produces: `func PlatformSnapshot(ctx context.Context, kind string, roots []string) (Snapshot, error)` for `//go:build linux` — the same exported name every platform file provides, so `main.go` references one symbol.

**`PlatformSnapshot` needs a job id for the tag, and its signature has none.** Derive it from the process: use `fmt.Sprintf("%d", os.Getpid())`. The tag's purpose is to identify *this agent's* snapshots for the sweep; a pid is sufficient and needs no signature change. Say so in the code comment.

- [ ] **Step 1: Write `snapshot_linux.go`**

```go
//go:build linux

package backup

import "context"

// PlatformSnapshot takes an LVM snapshot on Linux.
//
// The job id used in the tag is this process's pid: the tag exists so the
// orphan sweep can recognise snapshots THIS AGENT created, and a pid is enough
// for that. Threading the real job id would change Options.OpenSnapshot's
// signature for no gain.
func PlatformSnapshot(ctx context.Context, kind string, roots []string) (Snapshot, error) {
	if err := requireKind(kind, snapshotLVM); err != nil {
		return nil, err
	}
	return openLVM(ctx, execSnapRunner, pidTag(), roots)
}
```

Add `func pidTag() string { return strconv.Itoa(os.Getpid()) }` to `snapshot.go`.

- [ ] **Step 2: Write the skippable integration test**

Create `agent/internal/capabilities/backup/snapshot_lvm_integration_test.go` in `package backup`:

```go
// TestARealLVMSnapshotIsTakenAndReleased builds a loopback volume group, takes
// a real snapshot through openLVM, and verifies it presents the data.
//
// It SKIPS LOUDLY when the tooling is absent -- which it is in the standard
// development container. A skip that reads as a pass is how a gap survives.
func TestARealLVMSnapshotIsTakenAndReleased(t *testing.T) {
	for _, tool := range []string{"lvcreate", "lvs", "lvremove", "pvcreate", "vgcreate", "losetup", "mount", "umount"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skipf("SKIPPING the only end-to-end LVM proof: %q is not installed. "+
				"Nothing in this run exercised a real lvcreate.", tool)
		}
	}
	if os.Geteuid() != 0 {
		t.Skip("SKIPPING the only end-to-end LVM proof: not root, so no loopback volume group can be built.")
	}
	dir := t.TempDir()
	img := filepath.Join(dir, "pv.img")
	mustRun(t, "truncate", "-s", "256M", img)

	loop := strings.TrimSpace(mustOut(t, "losetup", "--find", "--show", img))
	t.Cleanup(func() { _ = exec.Command("losetup", "-d", loop).Run() })

	const vg = "toeshee-test-vg"
	mustRun(t, "pvcreate", "-f", loop)
	mustRun(t, "vgcreate", vg, loop)
	t.Cleanup(func() { _ = exec.Command("vgremove", "-f", vg).Run() })

	mustRun(t, "lvcreate", "-n", "origin", "-L", "64M", vg)
	origin := "/dev/" + vg + "/origin"
	mustRun(t, "mkfs.ext4", "-q", origin)

	mnt := filepath.Join(dir, "mnt")
	if err := os.MkdirAll(mnt, 0o755); err != nil {
		t.Fatal(err)
	}
	mustRun(t, "mount", origin, mnt)
	t.Cleanup(func() { _ = exec.Command("umount", mnt).Run() })

	want := []byte("payload")
	if err := os.WriteFile(filepath.Join(mnt, "canary.txt"), want, 0o600); err != nil {
		t.Fatal(err)
	}

	snap, err := openLVM(context.Background(), execSnapRunner, "itest", []string{mnt})
	if err != nil {
		t.Fatalf("openLVM against a real volume group = %v, want nil", err)
	}
	t.Cleanup(func() { _ = snap.Release() })

	got, err := os.ReadFile(snap.Translate(filepath.Join(mnt, "canary.txt")))
	if err != nil {
		t.Fatalf("reading the canary through the snapshot: %v", err)
	}
	if string(got) != string(want) {
		t.Fatalf("through the snapshot = %q, want %q", got, want)
	}
}
```

Add the two helpers in the same file: `mustRun(t, name, args...)` fails the test with the combined output on a non-zero exit, and `mustOut(t, name, args...)` returns stdout or fails the same way. Every resource gets its `t.Cleanup` at the moment it is created, so a failure part-way through still unwinds in reverse order.

- [ ] **Step 3: Run it and record which branch you got**

Run: `cd agent && go test ./internal/capabilities/backup/ -run TestARealLVMSnapshot -v -count=1`
Expected in this container: **SKIP**, naming the missing tool. Record the exact skip message in your report — that message is the artefact, since it is what a future reader will see instead of a proof.

- [ ] **Step 4: Verify the whole package still builds and passes**

Run: `cd agent && go build ./... && go vet ./... && go test ./internal/capabilities/backup/ -race -count=1`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git reset
git add agent/internal/capabilities/backup/snapshot_linux.go agent/internal/capabilities/backup/snapshot_lvm_integration_test.go agent/internal/capabilities/backup/snapshot.go
git commit -m "feat(agent/backup): Linux dispatch, and the one real-LVM proof

The integration test builds a loopback volume group and takes a real snapshot.
It skips loudly, naming the missing tool, where LVM is absent -- as it is in
the development container, so this run proved nothing about lvcreate itself."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 4: APFS logic and macOS dispatch

**Files:**
- Create: `agent/internal/capabilities/backup/snapshot_apfs.go` (NO build tag)
- Create: `agent/internal/capabilities/backup/snapshot_darwin.go`
- Test: `agent/internal/capabilities/backup/snapshot_apfs_test.go`

**Interfaces:**
- Consumes: Task 1's core; `pidTag` (Task 3).
- Produces: `func openAPFS(ctx context.Context, run snapRunner, tag string, roots []string) (Snapshot, error)`, `func sweepAPFSMountpoints(ctx context.Context, run snapRunner) error`, and `PlatformSnapshot` for `//go:build darwin`.

**The macOS orphan rule is deliberately different** (spec §3.2): `tmutil` names snapshots in Apple's format, so the agent cannot tag them, and a local snapshot may be one Time Machine relies on. The sweep reclaims **the agent's own mountpoints under `snapMountRoot`** and must **never** call `tmutil deletelocalsnapshots` on a snapshot it did not create.

- [ ] **Step 1: Write the failing tests**

In `snapshot_apfs_test.go`, reuse `fakeRun` from Task 2. Cover:

- `TestOpenAPFSMountsReadOnlyUnderTheAgentRoot` — assert the `mount_apfs` argv contains `-o` `ro` and a mountpoint under `snapMountRoot`.
- `TestSweepAPFSNeverDeletesASnapshotItDidNotCreate` — the fake reports two existing local snapshots; assert **no** `tmutil` call with `deletelocalsnapshots` appears in `f.calls`, and that only `umount` of paths under `snapMountRoot` is issued. Failure message: "deleting a Time Machine snapshot the agent did not create is worse than the leak".
- `TestOpenAPFSRefusesWhenTheSnapshotPresentsNoData` — the TCC simulation. Point `origin` at a populated `t.TempDir()` and `translated` at an empty one, and assert `openAPFS` refuses with a reason mentioning Full Disk Access.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agent && go test ./internal/capabilities/backup/ -run 'TestOpenAPFS|TestSweepAPFS' -count=1`
Expected: FAIL — `undefined: openAPFS`.

- [ ] **Step 3: Write `snapshot_apfs.go` and `snapshot_darwin.go`**

`openAPFS`: sweep first; `tmutil localsnapshot` and parse the created snapshot name from stdout; for each root, `mkdir -p` a mountpoint from `snapMountPath` and `mount_apfs -o ro -s <name> /System/Volumes/Data <mountpoint>`; build `volSnapshot`s whose teardown is `umount` **only**; then `verifyPresents`, releasing before returning any refusal.

`snapshot_darwin.go` is the thin dispatch, mirroring `snapshot_linux.go`:

```go
//go:build darwin

package backup

import "context"

func PlatformSnapshot(ctx context.Context, kind string, roots []string) (Snapshot, error) {
	if err := requireKind(kind, snapshotAPFS); err != nil {
		return nil, err
	}
	return openAPFS(ctx, execSnapRunner, pidTag(), roots)
}
```

`snapshot_darwin.go` carries the "never executed" header (see Task 5's wording, adapted).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agent && go test ./internal/capabilities/backup/ -race -count=1`
Expected: PASS.

- [ ] **Step 5: Falsify the TCC guard**

Remove the `verifyPresents` call from `openAPFS`. Confirm the edit applied, then run `TestOpenAPFSRefusesWhenTheSnapshotPresentsNoData`.
Expected: **FAIL** — the empty-snapshot case now succeeds. Report the output, revert, confirm the diff is empty.

- [ ] **Step 6: Verify the darwin build compiles**

Run: `cd agent && GOOS=darwin go vet ./... && GOOS=darwin go test -c -o /dev/null ./internal/capabilities/backup/`
Expected: both clean. Report the commands and output — this is the whole of `snapshot_darwin.go`'s verification.

- [ ] **Step 7: Commit**

```bash
git reset
git add agent/internal/capabilities/backup/snapshot_apfs.go agent/internal/capabilities/backup/snapshot_darwin.go agent/internal/capabilities/backup/snapshot_apfs_test.go
git commit -m "feat(agent/backup): APFS snapshot logic and macOS dispatch

The sweep reclaims only the agent's own mountpoints: tmutil names snapshots in
Apple's format, and deleting a local snapshot Time Machine relies on would be
worse than the leak. Falsified: removing the entry-count check makes the
Full-Disk-Access-denied case succeed with an empty tree."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 5: Windows VSS

**Files:**
- Create: `agent/internal/capabilities/backup/snapshot_windows.go`

**Interfaces:**
- Consumes: Task 1's core.
- Produces: `PlatformSnapshot` for `//go:build windows`.

**This task has no runnable test and cannot have one.** Its verification is `GOOS=windows go vet` and `go test -c`, which genuinely catches the failure mode `agent-engine.md` §4 names — a wrong vtable index or argument type does not compile. What it cannot catch is a right-shaped call with wrong semantics.

**Follow the sequence from `docs/modules/backup/agent-engine.md` §4 verbatim.** It is authoritative and more complete than any summary:

`CoInitializeEx` → `CreateVssBackupComponents` → `InitializeForBackup` → `SetContext(VSS_CTX_BACKUP)` → `GatherWriterMetadata` → `StartSnapshotSet` → `AddToSnapshotSet` per volume → `SetBackupState(false, false, VSS_BT_FULL)` → `PrepareForBackup` → `DoSnapshotSet` → `GetSnapshotProperties` for the device path. On the way out: `BackupComplete`, then `DeleteSnapshots` **for this snapshot set's id only**.

- [ ] **Step 1: Read the precedent**

Read `agent/internal/capabilities/patch/com_windows.go` in full before writing anything. It establishes the vtable-dispatch idiom, the `x/sys/windows` usage, and the header convention this file must match. Do not invent a second style.

- [ ] **Step 2: Write `snapshot_windows.go`**

Header first, in `com_windows.go`'s shape and stating at minimum:

```
// # THIS FILE HAS NEVER EXECUTED
//
// No VSS call in this file has run. This repository's CI is Linux, there is no
// Windows runner, and there is no Volume Shadow Copy Service to run it against.
// `GOOS=windows go vet` and `go test -c` type-check it and that is the whole of
// its verification -- a real check (a wrong vtable INDEX or argument type does
// not compile) but not a behavioural one.
//
// What would falsify it: a real backup.run on a Windows endpoint that returns
// succeeded with a manifest whose entries restore intact. Until that has
// happened, treat every claim here as documentation-derived.
//
// docs/modules/backup/agent-engine.md §4 previously judged writing this from
// documentation alone "not a risk worth taking". That judgement was overridden
// deliberately -- see docs/superpowers/specs/2026-08-10-snapshot-providers-design.md
// §3.1a -- and §4 records the change.
```

Then the implementation. `CreateVssBackupComponents` is a **name-mangled C++ export** in `vssapi.dll`; resolve it by its mangled name rather than the plain one. `Release()` performs `BackupComplete` then `DeleteSnapshots` scoped to this snapshot set's id, then `CoUninitialize`.

- [ ] **Step 3: Verify it compiles for Windows**

Run: `cd agent && GOOS=windows go vet ./... && GOOS=windows go test -c -o /dev/null ./internal/capabilities/backup/`
Expected: both clean.

- [ ] **Step 4: Verify the Linux build is untouched**

Run: `cd agent && go build ./... && go test ./internal/capabilities/backup/ -race -count=1`
Expected: PASS — the Windows file must not affect any other platform.

- [ ] **Step 5: Commit**

```bash
git reset
git add agent/internal/capabilities/backup/snapshot_windows.go
git commit -m "feat(agent/backup): VSS provider, never executed

The COM sequence is agent-engine.md §4's verbatim, including GatherWriterMetadata,
SetBackupState(false,false,VSS_BT_FULL), and the BackupComplete +
DeleteSnapshots-for-this-set-only teardown. Verified by GOOS=windows vet and a
compile-only test build; nothing here has run."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 6: Wire it, and record the override

**Files:**
- Modify: `agent/cmd/agent/main.go:260-262`
- Modify: `docs/modules/backup/agent-engine.md` §4
- Modify: `HANDOFF.md` §6
- Modify: `docs/modules/backup/spec.md` §6.1 item 4

**Interfaces:**
- Consumes: `PlatformSnapshot` from whichever platform file the build selects.

- [ ] **Step 1: Wire `main.go`**

`main.go:260` currently reads:

```go
	backup.Process.Bind(backup.Options{
		QueryChunks: replies.Query,
		OpenStore:   backupstore.Open,
```

Add `OpenSnapshot: backup.PlatformSnapshot,`. Leave `defaultOpenSnapshot` untouched: a nil field must keep refusing, so every test and any build that forgets to wire it refuses rather than silently snapshotting.

- [ ] **Step 2: Amend `agent-engine.md` §4**

§4 currently says a VSS binding written from documentation alone is "not a risk worth taking". That judgement was overridden this cycle. Rewrite the section to record: what was built, that the risk judgement changed, the three grounds (spec §3.1a), and what verification the Windows and macOS providers actually have. **Do not delete the original reasoning** — a future reader needs to know the position was held, and why it changed.

- [ ] **Step 3: Update `HANDOFF.md` §6 and backup spec §6.1 item 4**

Both currently list "no snapshot provider (BK-01)" as the reason `backup.run` refuses. Update to what is now true: Linux takes real LVM snapshots (logic tested, one real-LVM proof that skips where LVM is absent); Windows and macOS providers exist but **have never executed**; a root not on an LVM logical volume still refuses.

**Do not claim `backup.run` now completes.** It has not been run end to end on any platform.

- [ ] **Step 4: Full verification**

Run:
```
cd agent && go build ./... && go vet ./... && go test ./... -race -count=1
GOOS=windows go vet ./... && GOOS=windows go test -c -o /dev/null ./internal/capabilities/backup/
GOOS=darwin go vet ./... && GOOS=darwin go test -c -o /dev/null ./internal/capabilities/backup/
```
Expected: 22 packages ok; all cross-builds clean. `internal/capabilities/site` has a known pre-existing flaky RSS test — if it fails once and passes on rerun, say so.

- [ ] **Step 5: Commit**

```bash
git reset
git add agent/cmd/agent/main.go docs/modules/backup/agent-engine.md HANDOFF.md docs/modules/backup/spec.md
git commit -m "feat(agent): wire the snapshot providers; record the overridden decision

backup.run no longer refuses at the snapshot seam on Linux with an LVM root.
agent-engine.md §4's judgement that a documentation-only VSS binding was not
worth the risk is recorded as deliberately overridden, with the original
reasoning kept."
git push -u origin claude/handoff-spec-review-f45idd
```

---

## Verification

After Task 6:

```bash
cd agent && go build ./... && go vet ./... && go test ./... -race -count=1
```
Baseline: 22 packages ok. The backend and frontend suites are untouched by this plan.

## What this plan does not do

- It does not run a VSS or APFS call. Both are compile-only.
- It does not prove `backup.run` end to end on any platform.
- It does not make `backup.run` work on a Linux root that is not on an LVM logical volume.
- It does not touch CI, which is separately blocked behind ten pre-existing staticcheck findings.

package backup

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

func TestRequireKindRefusesAMismatchNamingBothSides(t *testing.T) {
	err := requireKind(snapshotLVM, snapshotVSS)
	if err == nil {
		t.Fatal("requireKind(lvm, vss) = nil, want a refusal")
	}
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("err is %T, want *jobs.Refusal: a wrong kind is a standing incapacity", err)
	}
	// Assert the two kinds land on the SIDE the sentence says they do, not
	// merely that both strings appear somewhere in the reason: a swapped
	// format argument (want and got transposed) would still contain both
	// substrings and pass a bare Contains check on each.
	wantReason := "backup.run: this endpoint provides the vss snapshot mechanism, but the job asked for lvm"
	if refusal.Reason != wantReason {
		t.Errorf("reason = %q, want %q -- provides/asked-for must name the platform's kind and the "+
			"job's kind respectively, not the other way round", refusal.Reason, wantReason)
	}
	if err := requireKind(snapshotVSS, snapshotVSS); err != nil {
		t.Fatalf("requireKind(vss, vss) = %v, want nil", err)
	}
}

// TestRootRelativeKeepsTheOffsetBetweenARootAndItsMountPoint is C1's shared
// half. A snapshot presents the FILESYSTEM's root, not the job's root: a root
// of /home/alice/docs on a filesystem mounted at /home appears inside the
// snapshot at <mount>/alice/docs. Discarding that offset backs up the whole
// snapshotted filesystem under the name of one subdirectory, and every
// existence check in this package still passes.
//
// This mirrors snapshot_windows.go's volumeRelative, which already had the
// correct model, including its path-BOUNDARY rule: /mnt/data must not swallow
// /mnt/database.
func TestRootRelativeKeepsTheOffsetBetweenARootAndItsMountPoint(t *testing.T) {
	for _, tc := range []struct {
		root, target, want string
		ok                 bool
	}{
		{"/home/alice/docs", "/home", "alice/docs", true},
		{"/home", "/home", "", true},
		{"/home/", "/home", "", true},
		{"/home/alice/docs", "/home/", "alice/docs", true},
		{"/data", "/", "data", true},
		{"/", "/", "", true},
		// The boundary rule. Without it the second of these maps into the
		// first's snapshot, which is C1 with a different first cause.
		{"/mnt/database", "/mnt/data", "", false},
		{"/elsewhere", "/home", "", false},
	} {
		got, ok := rootRelative(tc.root, tc.target)
		if ok != tc.ok || got != tc.want {
			t.Errorf("rootRelative(%q, %q) = %q, %v; want %q, %v",
				tc.root, tc.target, got, ok, tc.want, tc.ok)
		}
	}
}

// TestValidateRootsRefusesRelativeAndOptionLookingRootsWithoutNamingThem is
// I4 and I1 together.
//
// A relative root resolves against the service's working directory --
// restore.go:206 already refuses one for that reason. The leading-dash root is
// refused as a GUARD rather than against a live hazard: `findmnt --target`
// takes a required argument and getopt consumes it whatever it starts with, as
// checked with real findmnt on this container (see validateRoots' doc comment
// for the transcript). What the refusal protects against is a future call site
// that passes a root POSITIONALLY. This test asserts the refusal happens and
// stays path-free; it does not assert the old, false claim that the current
// tools misparse it.
//
// The refusal must identify the root POSITIONALLY: a refusal reason lands in
// the server's hash-chained audit log, and a customer's directory names do not
// belong there.
func TestValidateRootsRefusesRelativeAndOptionLookingRootsWithoutNamingThem(t *testing.T) {
	const marker = "ZZmarkerrootZZ"
	for _, tc := range []struct {
		name  string
		roots []string
		idx   string
	}{
		{"relative", []string{absRoot("/ok"), "relative/" + marker}, "1"},
		{"leading dash", []string{"-rf/" + marker}, "0"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := validateRoots(tc.roots)
			var refusal *jobs.Refusal
			if !errors.As(err, &refusal) {
				t.Fatalf("validateRoots(%v) = %v (%T), want a *jobs.Refusal", tc.roots, err, err)
			}
			if strings.Contains(refusal.Reason, marker) {
				t.Errorf("reason %q carries the root's own path into the audit log", refusal.Reason)
			}
			if !strings.Contains(refusal.Reason, tc.idx) {
				t.Errorf("reason %q does not identify the offending root by index %s", refusal.Reason, tc.idx)
			}
		})
	}
	if err := validateRoots([]string{absRoot("/a"), absRoot("/b/c")}); err != nil {
		t.Errorf("validateRoots(absolute roots) = %v, want nil", err)
	}
}

// absRoot spells a POSIX-shaped absolute path the way this OS does: unchanged
// on Unix, on drive C: on Windows.
func absRoot(posix string) string {
	if runtime.GOOS == "windows" {
		return "C:" + filepath.FromSlash(posix)
	}
	return posix
}

// validateRoots is shared with VSS, so "absolute" must mean what it means on
// the host: on Windows `\data` is rooted on whatever drive is current and
// `C:data` is relative to that drive's working directory -- neither maps onto
// one volume's shadow copy.
func TestValidateRootsUsesThisOSsIdeaOfAbsolute(t *testing.T) {
	ok, bad := []string{"/data", "/"}, []string{"data", `C:\Data`}
	if runtime.GOOS == "windows" {
		ok = []string{`C:\Data`, `c:\`, `\\server\share\data`}
		bad = []string{"/data", `\data`, `C:data`, "data"}
	}
	for _, r := range ok {
		if err := validateRoots([]string{r}); err != nil {
			t.Errorf("validateRoots(%q) = %v, want nil", r, err)
		}
	}
	for _, r := range bad {
		var refusal *jobs.Refusal
		if err := validateRoots([]string{r}); !errors.As(err, &refusal) {
			t.Errorf("validateRoots(%q) = %v, want a refusal", r, err)
		}
	}
}

// TestEmptyRootsIsRefusedBeforeAnythingIsCreated is Minor 5.
//
// openAPFS attached its teardown closure only at i == 0, so with ZERO roots
// `vols` was empty, Release was a no-op, and the `mount_apfs` it had already
// run was never unmounted -- a leak only the next run's sweep would catch.
// openVSS already guarded this case and the other two did not.
//
// The guard belongs in validateRoots, which all three providers call, so all
// three agree. Refusing there means nothing is created in the first place,
// which is a stronger fix than tearing down afterwards.
//
// Unreachable through `run` -- wire.go refuses an empty `sources.paths` --
// but reachable for any direct caller, which is what these are.
func TestEmptyRootsIsRefusedBeforeAnythingIsCreated(t *testing.T) {
	for _, roots := range [][]string{nil, {}} {
		err := validateRoots(roots)
		var refusal *jobs.Refusal
		if !errors.As(err, &refusal) {
			t.Fatalf("validateRoots(%v) = %v (%T), want a *jobs.Refusal", roots, err, err)
		}
	}
	// The shell-out providers' half is in snapshot_lvm_test.go: they are not
	// built on Windows.
}

// TestVerifyPresentsNamesBothMacOSCausesNotJustTCC is Minor 6.
//
// Both of verifyPresents' failures asserted a single cause: "on macOS this is
// what a missing Full Disk Access grant looks like". apfsRelative falls back to
// `/` for a root that is not under the APFS data volume, so a root on a
// separate volume (/Volumes/ExternalDisk/photos) translates to
// <mount>/Volumes/ExternalDisk/photos, which inside a data-volume snapshot is
// an empty mount-point directory. The run fails, correctly -- but with a reason
// that names the wrong cause, sending an operator to check a TCC grant that
// was never the problem.
//
// UNVERIFIED, like everything about macOS in this package: there is no Mac in
// this project. That is precisely why the reason must not assert ONE cause it
// cannot distinguish. This test pins that it names both.
func TestVerifyPresentsNamesBothMacOSCausesNotJustTCC(t *testing.T) {
	live := t.TempDir()
	if err := os.WriteFile(filepath.Join(live, "a"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}

	for _, tc := range []struct {
		name       string
		translated string
	}{
		// The offset inside the snapshot does not exist at all.
		{"translated path missing", filepath.Join(t.TempDir(), "absent")},
		// It exists and is empty while the live root is not.
		{"translated path empty", t.TempDir()},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := newMultiSnapshot([]volSnapshot{{origin: live, translated: tc.translated}})
			err := verifyPresents([]string{live}, s)
			if err == nil {
				t.Fatal("verifyPresents = nil, want a failure")
			}
			// Still an OPERATIONAL failure, not a refusal (§9).
			var refusal *jobs.Refusal
			if errors.As(err, &refusal) {
				t.Errorf("err = %q is a *jobs.Refusal; §9 routes this to a state:\"failed\" document", refusal.Reason)
			}
			if !strings.Contains(err.Error(), "Full Disk Access") {
				t.Errorf("error %q drops the Full Disk Access cause", err)
			}
			if !strings.Contains(err.Error(), "data volume") {
				t.Errorf("error %q names only the Full Disk Access cause. A root that is not on the APFS "+
					"data volume the snapshot covers fails here identically, and an operator told to "+
					"check a TCC grant will never find it", err)
			}
			if strings.Contains(err.Error(), live) || strings.Contains(err.Error(), tc.translated) {
				t.Errorf("error %q carries a path into backup_runs.error", err)
			}
		})
	}
}

// TestARootIndexIsDefinedWhereTheOperatorReadsIt is Important 2.
//
// Every provider names a rejected root by its INDEX rather than its path,
// because a path would land in the hash-chained audit log. The index is into
// the roots the agent works on -- sourcesSpec.roots() TRIMS, DE-DUPLICATES and
// SORTS the policy's `sources.paths` -- so it is NOT the policy's own ordering.
//
// Nothing the operator reads said so. The scenario: a policy naming
// ["/var/log", "/home/alice", "/home/alice"] normalizes to ["/home/alice",
// "/var/log"], the agent refuses index 1, and an operator who counts into
// their own policy investigates /home/alice when /var/log was the problem.
func TestARootIndexIsDefinedWhereTheOperatorReadsIt(t *testing.T) {
	// Part 1: the two orderings really do differ, so the definition is load
	// bearing rather than pedantry.
	policy := []string{"/var/log", "/home/alice", "/home/alice"}
	normalized, err := sourcesSpec{Paths: policy}.roots()
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"/home/alice", "/var/log"}; !reflect.DeepEqual(normalized, want) {
		t.Fatalf("roots() = %v, want %v", normalized, want)
	}
	if policy[1] == normalized[1] {
		t.Fatal("this test no longer demonstrates the mismatch it exists for")
	}

	// Part 2: the failure document an operator actually reads defines the
	// index. run()'s snapshot branch is the single funnel every provider
	// failure passes through on its way to `backup_runs.error`, so the
	// definition belongs there once rather than in each provider's message.
	svc := New(Options{
		OpenStore:   func(context.Context, Repository) (ObjectStore, error) { return newFakeStore(testStorageRef), nil },
		QueryChunks: newFakeDedup(testStorageRef).answer,
		OpenSnapshot: func(context.Context, string, []string) (Snapshot, error) {
			return nil, snapFailuref("the snapshot covering the source root at index 1 could not be mounted")
		},
	})
	res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, policy, nil))
	if err != nil {
		t.Fatalf("run returned a Go error (%v); an operational snapshot failure must be a "+
			"state:\"failed\" result document, or backup_runs stays on `running`", err)
	}
	if res.State != stateFailed {
		t.Fatalf("res.State = %q, want %q", res.State, stateFailed)
	}
	got := deref(res.Error)
	if !strings.Contains(got, "index 1") {
		t.Fatalf("error %q lost the provider's index", got)
	}
	for _, want := range []string{"de-duplicat", "sort"} {
		if !strings.Contains(got, want) {
			t.Errorf("error %q never says the index counts into the TRIMMED, DE-DUPLICATED, SORTED "+
				"source paths rather than the policy's own ordering. An operator counting into their "+
				"policy investigates the wrong root", got)
		}
	}
	// And it still names no path.
	for _, p := range policy {
		if strings.Contains(got, p) {
			t.Errorf("error %q carries a source path into backup_runs.error", got)
		}
	}
}

func TestTranslateResolvesThroughTheLongestMatchingPrefix(t *testing.T) {
	// Translate serves VSS too, so it works in this OS's own paths.
	s := newMultiSnapshot([]volSnapshot{
		{origin: absRoot("/data"), translated: absRoot("/snap/a")},
		{origin: absRoot("/data/inner"), translated: absRoot("/snap/b")},
	})
	for _, tc := range []struct{ in, want string }{
		{absRoot("/data/x"), absRoot("/snap/a/x")},
		{absRoot("/data/inner/y"), absRoot("/snap/b/y")},
		{absRoot("/data/innerx"), absRoot("/snap/a/innerx")},
		{absRoot("/elsewhere/z"), absRoot("/elsewhere/z")},
	} {
		if got := s.Translate(tc.in); got != tc.want {
			t.Errorf("Translate(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestReleaseContinuesPastAFailureAndReportsTheFirst(t *testing.T) {
	// Both teardowns fail here, with DISTINCT errors. That is deliberate: if
	// the second teardown returned nil (as an earlier version of this test
	// had it), removing the "&& first == nil" guard in Release would still
	// pass, because a nil second error can never overwrite a non-nil first
	// one regardless of the guard. Only two distinct non-nil errors expose
	// whether Release reports the FIRST one or the LAST one.
	var released []string
	first := errors.New("first teardown failed")
	second := errors.New("second teardown failed")
	s := newMultiSnapshot([]volSnapshot{
		{origin: "/a", translated: "/s/a", teardown: func() error { released = append(released, "a"); return first }},
		{origin: "/b", translated: "/s/b", teardown: func() error { released = append(released, "b"); return second }},
	})

	err := s.Release()

	if !errors.Is(err, first) {
		t.Errorf("Release() = %v, want the FIRST teardown's error", err)
	}
	if errors.Is(err, second) {
		t.Errorf("Release() = %v, reports the LAST teardown's error instead of the first", err)
	}
	if len(released) != 2 {
		t.Fatalf("released = %v, want both teardowns to run; an early return leaks the rest", released)
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

	// A snapshot that is POPULATED but has FEWER entries than live must NOT
	// refuse. This is normal on a running filesystem: a file can be created
	// between the snapshot and the count. Without this fixture, changing the
	// rule from `snapN == 0` to the over-strict `snapN < liveN` -- which would
	// fail every backup of a busy machine -- passes every other assertion in
	// this test unchanged, because every other fixture's snapshot count is
	// either 0 or exactly equal to live's.
	liveMany := t.TempDir()
	for _, name := range []string{"f1", "f2", "f3"} {
		if err := os.WriteFile(filepath.Join(liveMany, name), []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	snapFewer := t.TempDir()
	for _, name := range []string{"f1", "f2"} {
		if err := os.WriteFile(filepath.Join(snapFewer, name), []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := verifyPresents([]string{liveMany}, newMultiSnapshot([]volSnapshot{{origin: liveMany, translated: snapFewer}})); err != nil {
		t.Errorf("populated live (3) + populated snapshot (2) = %v, want nil: a smaller-but-nonzero "+
			"count is normal on a running filesystem and must not refuse", err)
	}
}

// TestVerifyPresentsFailsOperationallyAndNamesNoPath is I1 and I2 for the
// shared check. A snapshot that mounted and presents nothing is an
// OPERATIONAL failure of this run, not a standing incapacity of the device:
// agent-engine.md §9 routes that to a `state:"failed"` result document, which
// only happens if the error is NOT a *jobs.Refusal. And whichever way it
// travels -- audit log or `backup_runs.error` -- it must not carry the
// customer's directory name with it.
func TestVerifyPresentsFailsOperationallyAndNamesNoPath(t *testing.T) {
	const marker = "ZZmarkerrootZZ"
	live := filepath.Join(t.TempDir(), marker)
	if err := os.MkdirAll(live, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(live, "f"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	emptySnap := t.TempDir()

	err := verifyPresents([]string{live}, newMultiSnapshot([]volSnapshot{{origin: live, translated: emptySnap}}))
	if err == nil {
		t.Fatal("verifyPresents = nil for a populated root and an empty snapshot, want a failure")
	}
	var refusal *jobs.Refusal
	if errors.As(err, &refusal) {
		t.Errorf("verifyPresents returned a *jobs.Refusal (%q); an empty snapshot is transient, and a "+
			"refusal leaves the backup_runs row stuck on `running` with no error text (§9)", refusal.Reason)
	}
	if strings.Contains(err.Error(), marker) {
		t.Errorf("error %q carries the source root's own path", err)
	}
	if !strings.Contains(err.Error(), "index 0") {
		t.Errorf("error %q does not identify the root positionally", err)
	}
}

// TestATransientSnapshotFailureIsAFailedRunAndAStandingOneIsARefusal is I2 at
// the seam that decides it.
//
// agent-engine.md §9 splits `refused` (a standing incapacity of the device)
// from a `state:"failed"` RESULT DOCUMENT (transient or operational), and
// spells out the consequence of getting it wrong: a Go error for a transient
// condition produces a failed JOB, `ingest_backup_result` never runs, and the
// `backup_runs` row is stuck on `running` forever with no error text anywhere
// an operator looks. run.go's openSnapshot call site is what sorts them, by
// errors.As into *jobs.Refusal -- the same shape restore.go uses for GetRef
// and run.go already used for a root that cannot be walked.
func TestATransientSnapshotFailureIsAFailedRunAndAStandingOneIsARefusal(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "f"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}

	for _, tc := range []struct {
		name        string
		openErr     error
		wantRefusal bool
	}{
		{"the volume group is out of space", errors.New("an LVM snapshot could not be created"), false},
		{"no provider in this build", jobs.Refuse("backup.run: no LVM snapshot provider in this build"), true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			svc := New(Options{
				OpenStore: func(context.Context, Repository) (ObjectStore, error) {
					return newFakeStore(testStorageRef), nil
				},
				QueryChunks: newFakeDedup(testStorageRef).answer,
				OpenSnapshot: func(context.Context, string, []string) (Snapshot, error) {
					return nil, tc.openErr
				},
				Logger: quiet(),
			})

			res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{root}, nil))

			if tc.wantRefusal {
				var refusal *jobs.Refusal
				if !errors.As(err, &refusal) {
					t.Fatalf("run = %v, %v; want a *jobs.Refusal to travel through untouched", res, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("run returned the Go error %v; a transient snapshot failure must become a "+
					"result document, or ingest_backup_result never runs and the backup_runs row is "+
					"stuck on `running` with no error text", err)
			}
			if res == nil || res.State != stateFailed {
				t.Fatalf("run = %v, want a state=%q result document", res, stateFailed)
			}
			if res.Error == nil || *res.Error == "" {
				t.Fatal("the failed result carries no error text; an operator sees nothing")
			}
		})
	}
}

// TestExecSnapRunnerIsBoundedByAGrandchildThatInheritsTheOutputPipe is the
// measured falsifier for the WaitDelay fix (Important 1 of the task-1
// review). exec.CommandContext's cancellation kills only the DIRECT child; if
// that child has already backgrounded a grandchild that inherited its stdout
// pipe and exited, cmd.Wait blocks until the grandchild closes that pipe --
// which here would be 30s -- unless WaitDelay bounds it. This is the same
// finding patch/exec.go and internal/capabilities/inventory/exec.go record:
// "a 200ms deadline took 30.003s before this line existed."
func TestExecSnapRunnerIsBoundedByAGrandchildThatInheritsTheOutputPipe(t *testing.T) {
	requireSnapCommand(t, "sh")
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	started := time.Now()
	// sh backgrounds `sleep 30`, which inherits sh's stdout pipe, then sh
	// itself exits immediately -- the direct child is gone well before the
	// context deadline, but the grandchild keeps the pipe open for 30s.
	_ = execSnapRunner(ctx, nil, "sh", "-c", "sleep 30 & exit 0")
	if elapsed := time.Since(started); elapsed > 15*time.Second {
		t.Fatalf("execSnapRunner took %s; a 200ms context deadline plus a 10s WaitDelay must not "+
			"approach the grandchild's 30s sleep", elapsed)
	}
}

// TestExecSnapRunnerCapsWhatOneCommandMayContribute is the measured falsifier
// for the bounded-buffer fix (Important 2 of the task-1 review): a tool that
// writes forever must cost a fixed limit, not the agent's whole memory
// budget.
func TestExecSnapRunnerCapsWhatOneCommandMayContribute(t *testing.T) {
	requireSnapCommand(t, "sh")
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	// The pipeline must produce MORE than maxSnapCommandOutput, or the cap is
	// never actually exercised: patch's cap is 4 MiB (4,194,304 bytes), so an
	// 8,000,000-byte pipeline is comfortably over it.
	res := execSnapRunner(ctx, nil, "sh", "-c", "yes 0123456789abcdef | head -c 8000000")
	if len(res.Stdout) > maxSnapCommandOutput {
		t.Fatalf("execSnapRunner returned %d bytes of stdout; the cap is %d", len(res.Stdout), maxSnapCommandOutput)
	}
	if len(res.Stdout) == 0 {
		t.Skip("this shell produced nothing; the cap assertion above would be vacuous")
	}
}

func TestSnapBoundedBufferWriteReportsTheFullLengthWithNilError(t *testing.T) {
	// io.Writer requires n == len(p) whenever err == nil. A buffer that
	// truncates p and then returns len(p) -- the TRUNCATED length -- breaks
	// that contract for any write that starts with room left but whose length
	// exceeds it.
	//
	// FALSIFYING OBSERVATION: n != len(p) for such a write, or a non-nil
	// error.
	b := &snapBoundedBuffer{limit: 10}
	p := bytes.Repeat([]byte("A"), 100)
	n, err := b.Write(p)
	if err != nil {
		t.Fatalf("Write returned an error: %v, want nil", err)
	}
	if n != len(p) {
		t.Fatalf("Write(p) returned n=%d, want n=len(p)=%d -- io.Writer requires n==len(p) when err==nil",
			n, len(p))
	}
	if got := b.buf.Len(); got != 10 {
		t.Fatalf("buffered %d bytes, want the limit of 10 -- the cap must still hold the HEAD of the "+
			"input and drop the rest", got)
	}
}

func TestSnapBoundedBufferSurvivesIoCopyPastTheLimit(t *testing.T) {
	// The real failure mode: exec.Cmd copies a non-*os.File Stdout/Stderr
	// through an internal io.Copy goroutine in 32 KiB chunks. If Write ever
	// reports n < len(p) with a nil error, io.Copy turns that into a "short
	// write" error and cmd.Run() reports the process as failed even though it
	// ran and exited cleanly.
	//
	// FALSIFYING OBSERVATION: a non-nil error from io.Copy, or content beyond
	// the limit retained.
	b := &snapBoundedBuffer{limit: 10}
	src := bytes.NewReader(bytes.Repeat([]byte("B"), 100))
	copied, err := io.Copy(b, src)
	if err != nil {
		t.Fatalf("io.Copy returned an error: %v, want nil -- this is the short-write regression", err)
	}
	if copied != 100 {
		t.Fatalf("io.Copy reported %d bytes copied, want 100", copied)
	}
	if got := b.buf.Len(); got != 10 {
		t.Fatalf("buffered %d bytes, want the limit of 10", got)
	}
}

func requireSnapCommand(t *testing.T, name string) {
	t.Helper()
	if _, err := exec.LookPath(name); err != nil {
		t.Skipf("%s is not installed in this container", name)
	}
}

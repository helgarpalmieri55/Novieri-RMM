//go:build !windows

package backup

// Roots passed to openAPFS in these tests must be REAL directories, not
// fictional path strings, for the same reason snapshot_lvm_test.go's header
// gives: verifyPresents does a genuine os.Open outside the injected snapRunner
// seam. Use t.TempDir() for roots.
//
// The mirror trap exists on the OTHER side of that same call: verifyPresents
// also os.Opens the SNAPSHOT (translated) path when the root is non-empty, and
// openAPFS always computes that path under the real, package-level
// snapMountRoot -- which an unprivileged `go test` cannot write to. Every test
// here that needs a populated root therefore points apfsMountRoot (see
// snapshot_apfs.go) at a t.TempDir() first. Tests that leave the root empty
// never need to: verifyPresents short-circuits before it looks at the
// translated path at all, exactly as the LVM tests already rely on.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// withTempAPFSMountRoot points apfsMountRoot at a writable t.TempDir() for the
// duration of one test and restores it afterward. Tests never run in
// parallel in this file, so the shared package var is safe to swap this way.
func withTempAPFSMountRoot(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	orig := apfsMountRoot
	apfsMountRoot = dir
	t.Cleanup(func() { apfsMountRoot = orig })
	return dir
}

const fakeTMUtilCreateOutput = "Created local snapshot with date: 2024-01-15-142530\n"

func TestOpenAPFSMountsReadOnlyUnderTheAgentRoot(t *testing.T) {
	// Empty root: verifyPresents' liveN == 0 branch short-circuits, so this
	// test never needs the translated mountpoint to exist for real -- it is
	// purely about the mount_apfs argv, matching TestOpenLVMNamesTheSnapshot-
	// WithTheAgentTag's approach for the same reason.
	root := t.TempDir()

	f := &fakeRun{reply: func(name string, args []string) snapResult {
		if name == "tmutil" {
			return snapResult{Stdout: fakeTMUtilCreateOutput}
		}
		return snapResult{}
	}}

	if _, err := openAPFS(context.Background(), f.run, "job7", []string{root}); err != nil {
		t.Fatalf("openAPFS = %v, want nil", err)
	}

	var mountCall []string
	for _, c := range f.calls {
		if c[0] == "mount_apfs" {
			mountCall = c
		}
	}
	if mountCall == nil {
		t.Fatal("mount_apfs was never called")
	}

	var sawReadOnly bool
	for i, a := range mountCall {
		if a == "-o" && i+1 < len(mountCall) && mountCall[i+1] == "ro" {
			sawReadOnly = true
		}
	}
	if !sawReadOnly {
		t.Fatalf("mount_apfs args %v, want -o ro", mountCall)
	}

	mountPoint := mountCall[len(mountCall)-1]
	wantPrefix := snapMountRoot + string(filepath.Separator)
	if !strings.HasPrefix(mountPoint, wantPrefix) {
		t.Fatalf("mount_apfs mounted at %q, want it under %q", mountPoint, snapMountRoot)
	}
}

// TestAPFSRelativeMeasuresTheOffsetFromTheDataVolume is C1 for macOS.
//
// mount_apfs mounts the whole `/System/Volumes/Data` container, so what
// appears at the mountpoint is the DATA VOLUME's root, and a root's offset is
// measured from there -- not from the snapshot's mountpoint, which is what the
// provider used to translate every root to.
func TestAPFSRelativeMeasuresTheOffsetFromTheDataVolume(t *testing.T) {
	for _, tc := range []struct {
		root, want string
		ok         bool
	}{
		// Firmlinked: /Users is physically /System/Volumes/Data/Users, and a
		// policy names it the short way.
		{"/Users/alice/docs", "Users/alice/docs", true},
		// The same path spelled through the data volume's own mount point.
		{"/System/Volumes/Data/Users/alice/docs", "Users/alice/docs", true},
		{"/System/Volumes/Data", "", true},
		{"/", "", true},
	} {
		got, ok := apfsRelative(tc.root)
		if ok != tc.ok || got != tc.want {
			t.Errorf("apfsRelative(%q) = %q, %v; want %q, %v", tc.root, got, ok, tc.want, tc.ok)
		}
	}
}

// TestOpenAPFSTranslatesARootToItsOwnOffsetInsideTheSnapshot is C1's
// end-to-end half for macOS, and I3's: one container snapshot, ONE mount, and
// every root reading out of it at its own offset.
func TestOpenAPFSTranslatesARootToItsOwnOffsetInsideTheSnapshot(t *testing.T) {
	rootA, rootB := t.TempDir(), t.TempDir()

	f := &fakeRun{reply: func(name string, args []string) snapResult {
		if name == "tmutil" {
			return snapResult{Stdout: fakeTMUtilCreateOutput}
		}
		return snapResult{}
	}}

	snap, err := openAPFS(context.Background(), f.run, "job7", []string{rootA, rootB})
	if err != nil {
		t.Fatalf("openAPFS = %v, want nil", err)
	}

	var mounts int
	for _, c := range f.calls {
		if c[0] == "mount_apfs" {
			mounts++
		}
	}
	if mounts != 1 {
		t.Errorf("mount_apfs ran %d time(s) for two roots; one local snapshot of the whole container "+
			"serves every root, so mounting it once is enough and every extra mount is one more thing "+
			"to leak", mounts)
	}

	mountPoint := apfsSnapMountPoint("job7", 0)
	for _, root := range []string{rootA, rootB} {
		rel, ok := apfsRelative(root)
		if !ok {
			t.Fatalf("apfsRelative(%q) = _, false", root)
		}
		want := filepath.Join(mountPoint, rel)
		if got := snap.Translate(root); got != want {
			t.Errorf("Translate(%q) = %q, want %q. Translating to the snapshot's mount root backs up "+
				"the whole data volume under the name of one subdirectory", root, got, want)
		}
	}
}

// TestOpenAPFSRefusalsAndFailuresNameNoPath is I1 and I2 for this provider.
func TestOpenAPFSRefusalsAndFailuresNameNoPath(t *testing.T) {
	const marker = "ZZmarkerrootZZ"
	for _, tc := range []struct {
		name  string
		reply func(string, []string) snapResult
	}{
		{"tmutil localsnapshot fails", func(name string, args []string) snapResult {
			if name == "tmutil" {
				return snapResult{ExitCode: 1, Err: errors.New("no space"),
					Stderr: "cannot snapshot " + marker}
			}
			return snapResult{}
		}},
		{"mount_apfs fails", func(name string, args []string) snapResult {
			switch name {
			case "tmutil":
				return snapResult{Stdout: fakeTMUtilCreateOutput}
			case "mount_apfs":
				return snapResult{ExitCode: 1, Err: errors.New("mount failed")}
			}
			return snapResult{}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := filepath.Join(t.TempDir(), marker)
			if err := os.MkdirAll(root, 0o700); err != nil {
				t.Fatal(err)
			}
			f := &fakeRun{reply: tc.reply}
			_, err := openAPFS(context.Background(), f.run, "job1", []string{root})
			if err == nil {
				t.Fatal("openAPFS = nil, want an error")
			}
			if strings.Contains(err.Error(), marker) {
				t.Errorf("error %q carries a source path (or a command's echo of one) into the audit "+
					"log or backup_runs.error", err)
			}
			var refusal *jobs.Refusal
			if errors.As(err, &refusal) {
				t.Errorf("err = %q is a *jobs.Refusal; tmutil and mount_apfs failing are operational, "+
					"not standing incapacities of the device (§9)", refusal.Reason)
			}
		})
	}
}

// TestSweepAPFSNeverDeletesASnapshotItDidNotCreate is the macOS orphan rule's
// falsifier. tmutil names local snapshots in Apple's own format, not this
// agent's, and a local snapshot may be one Time Machine itself relies on -- so
// unlike LVM, the sweep must reclaim ONLY the agent's own mountpoints under
// snapMountRoot, and must never call tmutil deletelocalsnapshots on anything.
func TestSweepAPFSNeverDeletesASnapshotItDidNotCreate(t *testing.T) {
	agentMount1 := filepath.Join(snapMountRoot, snapTagPrefix+"old-0")
	agentMount2 := filepath.Join(snapMountRoot, snapTagPrefix+"old-1")

	f := &fakeRun{reply: func(name string, args []string) snapResult {
		switch name {
		case "mount":
			return snapResult{Stdout: strings.Join([]string{
				"/dev/disk1s1 on / (apfs, local, journaled)",
				"/dev/disk1s5s1 on " + agentMount1 + " (apfs, local, read-only, journaled, nobrowse)",
				"/dev/disk1s5s1 on " + agentMount2 + " (apfs, local, read-only, journaled, nobrowse)",
				"map auto_home on /home (autofs, automounted, nobrowse)",
			}, "\n") + "\n"}
		case "tmutil":
			// Two existing Time Machine local snapshots the agent did NOT
			// create. If the sweep queries tmutil at all, it must never ask
			// to delete either of these.
			return snapResult{Stdout: "com.apple.TimeMachine.2024-01-15-090000.local\n" +
				"com.apple.TimeMachine.2024-01-15-100000.local\n"}
		}
		return snapResult{}
	}}

	if err := sweepAPFSMountpoints(context.Background(), f.run); err != nil {
		t.Fatalf("sweepAPFSMountpoints = %v, want nil", err)
	}

	var umounted []string
	for _, c := range f.calls {
		switch c[0] {
		case "tmutil":
			for _, a := range c {
				if a == "deletelocalsnapshots" {
					t.Fatal("sweepAPFSMountpoints called tmutil deletelocalsnapshots: deleting a Time " +
						"Machine snapshot the agent did not create is worse than the leak it fixes")
				}
			}
		case "umount":
			if len(c) < 2 {
				t.Fatalf("umount call %v is missing a target", c)
			}
			umounted = append(umounted, c[1])
		}
	}

	sort.Strings(umounted)
	want := []string{agentMount1, agentMount2}
	if !reflect.DeepEqual(umounted, want) {
		t.Fatalf("umounted %v, want exactly %v: the sweep must reclaim only the agent's own mountpoints "+
			"under %s and touch nothing else", umounted, want, snapMountRoot)
	}
}

// TestOpenAPFSRefusesWhenTheSnapshotPresentsNoData is the TCC simulation:
// without a Full Disk Access grant, mount_apfs succeeds and the mounted tree
// reads empty. Without the verifyPresents check this would complete, upload
// almost nothing, and report success.
func TestOpenAPFSRefusesWhenTheSnapshotPresentsNoData(t *testing.T) {
	withTempAPFSMountRoot(t)

	origin := t.TempDir()
	if err := os.WriteFile(filepath.Join(origin, "Documents"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}

	// The path openAPFS will translate the root to, pre-created as REAL and
	// EMPTY -- exactly what mount_apfs leaves behind when Full Disk Access is
	// denied. Since C1 that is the mountpoint PLUS the root's offset inside
	// the data volume, not the bare mountpoint: creating only the mountpoint
	// would exercise the "path does not exist" branch instead of the
	// "exists and is empty" one this test is about.
	mountPoint := apfsSnapMountPoint("tcc", 0)
	rel, ok := apfsRelative(origin)
	if !ok {
		t.Fatalf("apfsRelative(%q) = _, false", origin)
	}
	if err := os.MkdirAll(filepath.Join(mountPoint, rel), 0o700); err != nil {
		t.Fatal(err)
	}

	f := &fakeRun{reply: func(name string, args []string) snapResult {
		if name == "tmutil" {
			return snapResult{Stdout: fakeTMUtilCreateOutput}
		}
		return snapResult{}
	}}

	_, err := openAPFS(context.Background(), f.run, "tcc", []string{origin})

	if err == nil {
		t.Fatal("openAPFS = nil for a snapshot that presents nothing, want a failure")
	}
	// A FAILURE, not a refusal: §9 routes an operational condition into a
	// `state:"failed"` result document, and only a *jobs.Refusal travels as a
	// refusal. A refusal here would leave the backup_runs row on `running`.
	var refusal *jobs.Refusal
	if errors.As(err, &refusal) {
		t.Errorf("openAPFS err = %q is a *jobs.Refusal", refusal.Reason)
	}
	if !strings.Contains(err.Error(), "Full Disk Access") {
		t.Fatalf("error = %q, want it to mention Full Disk Access -- this is what catches an "+
			"unmounted-but-succeeded macOS snapshot that would otherwise report success having backed up "+
			"nothing", err)
	}

	// A refusal here must not leak the mount just made.
	for _, c := range f.calls {
		if c[0] == "umount" && len(c) > 1 && c[1] == mountPoint {
			return
		}
	}
	t.Fatalf("openAPFS refused but never released (umounted) %q; a refusal must not leak every mount just made", mountPoint)
}

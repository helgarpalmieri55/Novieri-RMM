//go:build linux

package backup

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// The live fallback on a real Linux filesystem, through the production
// PlatformSnapshot: real findmnt, real lvs (or its absence), a real walk, and
// a real restore compared byte for byte. It runs on ubuntu-latest, whose
// temporary directory is on a plain ext4 root.

// requirePlainFilesystem skips unless the temp dir is on a filesystem `auto`
// reads live, which is what this file tests; on btrfs, ZFS or an LV the same
// run would take a snapshot and need root.
func requirePlainFilesystem(t *testing.T, dir string) {
	t.Helper()
	if _, err := exec.LookPath("findmnt"); err != nil {
		t.Skip("findmnt is not installed; auto selection cannot resolve a filesystem here")
	}
	mi, ok := findMount(context.Background(), execSnapRunner, dir)
	if !ok {
		t.Skip("findmnt could not resolve the temporary directory")
	}
	if mi.fstype == "btrfs" || mi.fstype == "zfs" || onLogicalVolume(context.Background(), execSnapRunner, mi.source) {
		t.Skipf("the temporary directory is on %s (%s); this test covers the live fallback only",
			mi.fstype, mi.source)
	}
}

func childPIDs(t *testing.T) []string {
	t.Helper()
	tasks, err := filepath.Glob("/proc/self/task/*/children")
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, f := range tasks {
		b, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		out = append(out, strings.Fields(string(b))...)
	}
	return out
}

func TestARealLiveFallbackRoundTripsAndIsFlagged(t *testing.T) {
	src := buildFixture(t)
	requirePlainFilesystem(t, src)
	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	svc.opts.OpenSnapshot = PlatformSnapshot

	var doc map[string]any
	_ = json.Unmarshal(v2RunParams(t, []string{src}, nil), &doc)
	doc["snapshot"] = snapshotAuto
	raw, _ := json.Marshal(doc)
	o, err := svc.execRun(context.Background(), testJobID, raw)
	if err != nil {
		t.Fatal(err)
	}
	if o.res.State != stateSuccess {
		t.Fatalf("state %s: %s", o.res.State, deref(o.res.Error))
	}
	w := o.wire().(*runResultV2)
	if w.Consistency.Level != levelLive || w.Consistency.CrashConsistent || w.Consistency.Snapshot != mechNone {
		t.Fatalf("consistency = %+v", w.Consistency)
	}
	var flagged bool
	for _, x := range w.Warnings {
		flagged = flagged || x == warnNotSnapshotConsistent
	}
	if !flagged {
		t.Fatalf("a live backup is not flagged: warnings %v", w.Warnings)
	}
	out := restoreAll(t, svc, store, o, src)
	before, after := walkTree(t, src), walkTree(t, out)
	if len(before) != len(after) {
		t.Fatalf("%d entries backed up, %d restored", len(before), len(after))
	}
	for rel, want := range before {
		got := after[rel]
		if got.kind != want.kind || !bytes.Equal(got.content, want.content) || got.target != want.target ||
			(got.kind != kindSymlink && got.mode != want.mode) {
			t.Errorf("%s restored differently", rel)
		}
	}
}

func TestFalsifierB6_NoChildProcessOutlivesALiveModeRun(t *testing.T) {
	// FALSIFYING OBSERVATION: any child of this process alive after the job
	// returns (§10 B6). The provider runs findmnt and lvs; each must be reaped.
	src := t.TempDir()
	requirePlainFilesystem(t, src)
	if err := os.WriteFile(filepath.Join(src, "f"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if kids := childPIDs(t); len(kids) != 0 {
		t.Skipf("children already running before the job: %v", kids)
	}
	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	svc.opts.OpenSnapshot = PlatformSnapshot
	var doc map[string]any
	_ = json.Unmarshal(v2RunParams(t, []string{src}, nil), &doc)
	doc["snapshot"] = snapshotAuto
	raw, _ := json.Marshal(doc)
	o, err := svc.execRun(context.Background(), testJobID, raw)
	if err != nil || o.res.State != stateSuccess {
		t.Fatalf("%v %+v", err, o)
	}
	if kids := childPIDs(t); len(kids) != 0 {
		t.Fatalf("falsifier B6: child processes alive after the job: %v", kids)
	}
	t.Logf("falsifier B6: 0 child processes after a live-mode run through the real provider")
}

func TestARealLiveRootIsRefusedWhenTheWorkloadForbidsLiveReads(t *testing.T) {
	src := t.TempDir()
	requirePlainFilesystem(t, src)
	snap, err := PlatformSnapshot(withSnapOptions(context.Background(), snapOptions{allowLive: false}),
		snapshotAuto, []string{src})
	var r *jobs.Refusal
	if snap != nil || !errors.As(err, &r) || !strings.Contains(r.Reason, "live read") {
		t.Fatalf("snap %v err %v", snap, err)
	}
}

func TestPlatformSnapshotOnLinuxDispatchesAutoToTheAutoProvider(t *testing.T) {
	_, err := PlatformSnapshot(context.Background(), snapshotAuto, []string{"not/absolute"})
	var r *jobs.Refusal
	if !errors.As(err, &r) || !strings.Contains(r.Reason, "is not an absolute path") {
		t.Fatalf("err = %v; auto must reach openAuto's validateRoots", err)
	}
}

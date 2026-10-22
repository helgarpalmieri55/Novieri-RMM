package backup

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Live read with change detection (spec-server-backup.md §4.4): the fallback
// when a Linux root has no snapshot mechanism, on by default (operator
// decision 2026-09-23) and flagged "not crash-consistent" in the result.

// mutatingFS is the real filesystem, plus a hook on every open of one file.
// The walk's reads and the dedup re-read both open through it.
type mutatingFS struct {
	*osTreeFS
	target string
	opens  int
	onOpen func(n int, p string)
}

func (m *mutatingFS) open(p string) (sourceFile, error) {
	f, err := m.osTreeFS.open(p)
	if err == nil && p == m.target {
		m.opens++
		m.onOpen(m.opens, p)
	}
	return f, err
}

func liveService(store *fakeStore, fsys treeFS, mech string) *Service {
	svc := newTestService(store, quiet())
	svc.opts.OpenSnapshot = func(_ context.Context, _ string, roots []string) (Snapshot, error) {
		vols := make([]volSnapshot, 0, len(roots))
		for _, r := range roots {
			vols = append(vols, volSnapshot{origin: r, translated: r, mech: mech})
		}
		return newMultiSnapshot(vols), nil
	}
	svc.treeFS = func() treeFS { return fsys }
	return svc
}

func appendTo(t *testing.T, p, s string) {
	t.Helper()
	f, err := os.OpenFile(p, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString(s); err != nil {
		t.Fatal(err)
	}
	_ = f.Close()
}

func liveFixture(t *testing.T, size int) (src, target string) {
	t.Helper()
	src = filepath.Join(t.TempDir(), "live")
	if err := os.MkdirAll(src, 0o755); err != nil {
		t.Fatal(err)
	}
	target = filepath.Join(src, "app.log")
	content := make([]byte, size)
	fill(content, 7)
	if err := os.WriteFile(target, content, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(src, "still.txt"), []byte("not changing"), 0o644); err != nil {
		t.Fatal(err)
	}
	return src, target
}

func liveRun(t *testing.T, svc *Service, src string) *runOutcome {
	t.Helper()
	var doc = v2RunParams(t, []string{src}, nil)
	o, err := svc.execRun(context.Background(), testJobID, doc)
	if err != nil {
		t.Fatal(err)
	}
	return o
}

func restoreAll(t *testing.T, svc *Service, store *fakeStore, o *runOutcome, src string) string {
	t.Helper()
	dest := filepath.Join(t.TempDir(), "r")
	ro, err := svc.execRestore(context.Background(), testJobID, v2RestoreParams(t,
		store.prefix+*o.res.ManifestRef, []string{src}, dest, modeRestore, nil))
	if err != nil || ro.res.State != stateSuccess || len(ro.res.Mismatch) != 0 {
		t.Fatalf("restore: %v %+v", err, ro.res)
	}
	return expectedDest(dest, src)
}

func entryFor(t *testing.T, store *fakeStore, o *runOutcome, p string) (entryV2, bool) {
	t.Helper()
	_, m := readManifestDoc(t, store, *o.res.ManifestRef)
	for _, e := range v2Entries(t, store, m) {
		if e.Path == filepath.ToSlash(p) {
			return e, true
		}
	}
	return entryV2{}, false
}

func TestAFileStillChangingAfterThreeReadsIsKeptAndFlagged(t *testing.T) {
	src, target := liveFixture(t, 3000)
	fsys := &mutatingFS{osTreeFS: newOSTreeFS(), target: target, onOpen: func(n int, p string) {
		if n <= liveReadAttempts {
			appendTo(t, p, "+")
		}
	}}
	store := newFakeStore(testStorageRef)
	svc := liveService(store, fsys, mechNone)
	o := liveRun(t, svc, src)
	if o.res.State != stateSuccess {
		t.Fatalf("state %s: %v", o.res.State, deref(o.res.Error))
	}
	if fsys.opens < liveReadAttempts {
		t.Fatalf("the file was opened %d times; a changing file is read up to three times", fsys.opens)
	}
	w := o.wire().(*runResultV2)
	c := w.Consistency
	if c.Level != levelLive || c.CrashConsistent || c.Snapshot != mechNone || c.FilesChangedDuringRead != 1 {
		t.Fatalf("consistency = %+v", c)
	}
	if strings.Join(w.Warnings, ",") != warnNotSnapshotConsistent+","+warnChangedDuringRead {
		t.Errorf("warnings = %v", w.Warnings)
	}
	e, ok := entryFor(t, store, o, target)
	if !ok || !e.hasFlag(flagChangedDuringRead) {
		t.Fatalf("entry %+v is not flagged changed_during_read", e)
	}
	still, _ := entryFor(t, store, o, filepath.Join(src, "still.txt"))
	if still.hasFlag(flagChangedDuringRead) {
		t.Error("an unchanged file was flagged")
	}
	out := restoreAll(t, svc, store, o, src)
	want, _ := os.ReadFile(target)
	if got, _ := os.ReadFile(filepath.Join(out, "app.log")); !bytes.Equal(got, want) {
		t.Fatalf("restored %d bytes, the last read saw %d", len(got), len(want))
	}
}

func TestAFileThatSettlesIsReReadAndNotFlagged(t *testing.T) {
	src, target := liveFixture(t, 3000)
	fsys := &mutatingFS{osTreeFS: newOSTreeFS(), target: target, onOpen: func(n int, p string) {
		if n == 1 {
			appendTo(t, p, "late write")
		}
	}}
	store := newFakeStore(testStorageRef)
	svc := liveService(store, fsys, mechNone)
	o := liveRun(t, svc, src)
	if o.res.State != stateSuccess || o.cons.FilesChangedDuringRead != 0 {
		t.Fatalf("%+v %+v", o.res, o.cons)
	}
	if e, _ := entryFor(t, store, o, target); e.hasFlag(flagChangedDuringRead) {
		t.Fatal("a file consistent on its second read was flagged")
	}
	if o.res.BytesProtected != 3000+int64(len("late write"))+int64(len("not changing")) {
		t.Errorf("bytes_protected %d counts the discarded read", o.res.BytesProtected)
	}
	out := restoreAll(t, svc, store, o, src)
	want, _ := os.ReadFile(target)
	if got, _ := os.ReadFile(filepath.Join(out, "app.log")); !bytes.Equal(got, want) {
		t.Fatal("the restored file is not the settled one")
	}
}

func TestALiveFileRewrittenBeforeUploadIsStoredUnderItsNewIDs(t *testing.T) {
	// The chunk ids were computed on the first read; the upload re-reads. A
	// live file can change between the two. Under a snapshot that would be
	// corruption and fails the run; live, the bytes are stored under their own
	// id and the entry rewritten, so every chunk the manifest names exists and
	// hashes right.
	src, target := liveFixture(t, 4000)
	replacement := make([]byte, 4000)
	fill(replacement, 99)
	fsys := &mutatingFS{osTreeFS: newOSTreeFS(), target: target, onOpen: func(n int, p string) {
		if n == 2 { // the dedup re-read
			if err := os.WriteFile(p, replacement, 0o644); err != nil {
				t.Fatal(err)
			}
		}
	}}
	store := newFakeStore(testStorageRef)
	svc := liveService(store, fsys, mechNone)
	o := liveRun(t, svc, src)
	if o.res.State != stateSuccess {
		t.Fatalf("state %s: %v", o.res.State, deref(o.res.Error))
	}
	if o.cons.FilesChangedDuringRead != 1 {
		t.Errorf("files_changed_during_read = %d", o.cons.FilesChangedDuringRead)
	}
	if e, _ := entryFor(t, store, o, target); !e.hasFlag(flagChangedDuringRead) {
		t.Error("the rewritten entry is not flagged")
	}
	out := restoreAll(t, svc, store, o, src)
	if got, _ := os.ReadFile(filepath.Join(out, "app.log")); !bytes.Equal(got, replacement) {
		t.Fatalf("restored bytes are neither version (%d bytes)", len(got))
	}
}

func TestTheSameTornReReadUnderASnapshotFailsTheRun(t *testing.T) {
	src, target := liveFixture(t, 4000)
	fsys := &mutatingFS{osTreeFS: newOSTreeFS(), target: target, onOpen: func(n int, p string) {
		if n == 2 {
			other := make([]byte, 4000)
			fill(other, 5)
			_ = os.WriteFile(p, other, 0o644)
		}
	}}
	store := newFakeStore(testStorageRef)
	svc := liveService(store, fsys, mechLVM)
	o := liveRun(t, svc, src)
	if o.res.State != stateFailed || !strings.Contains(deref(o.res.Error), "changed between being hashed") {
		t.Fatalf("a torn read under a snapshot did not fail the run: %+v %v", o.res, deref(o.res.Error))
	}
}

func TestALiveFileTruncatedBeforeUploadIsDroppedNotCorrupted(t *testing.T) {
	src, target := liveFixture(t, 4000)
	fsys := &mutatingFS{osTreeFS: newOSTreeFS(), target: target, onOpen: func(n int, p string) {
		if n == 2 {
			_ = os.Truncate(p, 10)
		}
	}}
	store := newFakeStore(testStorageRef)
	svc := liveService(store, fsys, mechNone)
	o := liveRun(t, svc, src)
	if o.res.State != stateSuccess {
		t.Fatalf("%+v %v", o.res, deref(o.res.Error))
	}
	if _, ok := entryFor(t, store, o, target); ok {
		t.Fatal("a file with no consistent copy this run is in the manifest")
	}
	if n, _ := o.res.DedupStats["unreadable_files"].(int64); n != 1 {
		t.Errorf("unreadable_files = %v", o.res.DedupStats["unreadable_files"])
	}
	if n, _ := o.res.DedupStats["files"].(int64); n != 1 {
		t.Errorf("files = %v; the dropped file is still counted", o.res.DedupStats["files"])
	}
}

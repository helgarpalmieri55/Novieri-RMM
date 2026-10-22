package backup

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Format 2 end to end: a paged manifest written by the streaming walk, read
// back by the paged restore.

func v2RestoreParams(t *testing.T, ref string, selection []string, target, mode string,
	extraTarget map[string]any) json.RawMessage {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal(restoreParamsJSON(t, ref, selection, target, mode), &doc); err != nil {
		t.Fatal(err)
	}
	doc["format"] = 2
	tgt := doc["target"].(map[string]any)
	for k, v := range extraTarget {
		tgt[k] = v
	}
	raw, _ := json.Marshal(doc)
	return raw
}

func readManifestDoc(t *testing.T, store *fakeStore, suffix string) (*backupManifest, *manifestV2) {
	t.Helper()
	key, err := decodeRepoKey(testKey)
	if err != nil {
		t.Fatal(err)
	}
	blob, err := store.Get(context.Background(), suffix)
	if err != nil {
		t.Fatal(err)
	}
	m1, m2, err := openManifestDoc(key, blob)
	if err != nil {
		t.Fatal(err)
	}
	return m1, m2
}

// v2Entries reads every entry of a v2 manifest out of the fake store.
func v2Entries(t *testing.T, store *fakeStore, m *manifestV2) []entryV2 {
	t.Helper()
	key, _ := decodeRepoKey(testKey)
	var out []entryV2
	for _, st := range m.Streams {
		for _, pid := range st.Pages {
			data, err := fetchPage(context.Background(), store, key, newBlake3(), pid)
			if err != nil {
				t.Fatal(err)
			}
			es, err := parseEntryPage(data)
			if err != nil {
				t.Fatal(err)
			}
			out = append(out, es...)
		}
	}
	return out
}

func TestAFormat2PointRoundTripsThroughAPagedManifest(t *testing.T) {
	src := buildFixture(t)
	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	ctx := context.Background()

	var doc map[string]any
	_ = json.Unmarshal(v2RunParams(t, []string{src}, nil), &doc)
	doc["snapshot"] = snapshotLVM
	raw, _ := json.Marshal(doc)
	o, err := svc.execRun(ctx, testJobID, raw)
	if err != nil {
		t.Fatal(err)
	}
	res := o.res
	if res.State != stateSuccess {
		t.Fatalf("state=%s error=%v", res.State, deref(res.Error))
	}
	_, m := readManifestDoc(t, store, *res.ManifestRef)
	if m == nil {
		t.Fatal("a format-2 run wrote a schema_version 1 manifest")
	}
	if m.SchemaVersion != 2 || len(m.Streams) != 1 || len(m.Streams[0].Pages) < 2 ||
		m.Header.WorkloadKind != workloadFiles || m.Header.Consistency == nil ||
		len(m.Header.PageIndex) != len(m.Streams[0].Pages) {
		t.Fatalf("manifest = %+v", m)
	}
	pages, _ := res.DedupStats["manifest_pages"].(int64)
	if got := store.chunkCount(); got != int(res.ChunkCount)+int(pages) {
		t.Errorf("the repository holds %d chunk objects; chunk_count %d + manifest_pages %d", got,
			res.ChunkCount, pages)
	}
	entries := v2Entries(t, store, m)
	if int64(len(entries)) != m.Streams[0].EntryCount {
		t.Fatalf("%d entries in pages, entry_count %d", len(entries), m.Streams[0].EntryCount)
	}
	// Walk order: every parent precedes its children.
	seenDirs := map[string]bool{}
	var bigFile bool
	for _, e := range entries {
		if dir := filepath.ToSlash(filepath.Dir(filepath.FromSlash(e.Path))); e.Path != filepath.ToSlash(src) &&
			!seenDirs[dir] {
			t.Fatalf("%s came before its parent", e.Path)
		}
		if e.Kind == kindDir {
			seenDirs[e.Path] = true
		}
		if len(e.ChunkPages) > 0 {
			bigFile = true
		}
	}
	if !bigFile {
		t.Error("no entry moved its chunk ids to id pages; the fixture does not exercise them")
	}

	// The result's format-2 members.
	w := o.wire().(*runResultV2)
	if w.Consistency.Level != levelSnapshot || !w.Consistency.CrashConsistent ||
		w.Consistency.Snapshot != mechLVM || len(w.Warnings) != 0 {
		t.Errorf("consistency %+v warnings %v", w.Consistency, w.Warnings)
	}

	// ---- restore ----------------------------------------------------------
	dest := filepath.Join(t.TempDir(), "restored")
	ro, err := svc.execRestore(ctx, testJobID, v2RestoreParams(t, store.prefix+*res.ManifestRef,
		[]string{src}, dest, modeRestore, nil))
	if err != nil {
		t.Fatal(err)
	}
	if ro.res.State != stateSuccess || len(ro.res.Mismatch) != 0 {
		t.Fatalf("restore state=%s mismatch=%v error=%v", ro.res.State, ro.res.Mismatch, deref(ro.res.Error))
	}
	before := walkTree(t, src)
	after := walkTree(t, expectedDest(dest, src))
	if len(before) != len(after) {
		t.Errorf("%d entries in, %d restored", len(before), len(after))
	}
	for rel, want := range before {
		got, ok := after[rel]
		if !ok {
			t.Errorf("%s missing", rel)
			continue
		}
		if got.kind != want.kind || (got.kind != kindSymlink && got.mode != want.mode) ||
			!bytes.Equal(got.content, want.content) || got.target != want.target {
			t.Errorf("%s restored as %+v, was %+v", rel, got.kind, want.kind)
		}
	}
	// mtimes come back, including on directories (applied after their
	// contents), to the float's resolution (about half a microsecond today).
	for _, rel := range []string{"large-100k.bin", "empty-dir", "a/b"} {
		a, errA := os.Stat(filepath.Join(src, rel))
		b, errB := os.Stat(filepath.Join(expectedDest(dest, src), filepath.FromSlash(rel)))
		if errA != nil || errB != nil {
			t.Fatalf("%v %v", errA, errB)
		}
		if d := a.ModTime().Sub(b.ModTime()); d > 2*time.Microsecond || d < -2*time.Microsecond {
			t.Errorf("%s mtime %v, was %v", rel, b.ModTime(), a.ModTime())
		}
	}
	rw := ro.wire().(*restoreResultV2)
	if rw.Warnings == nil {
		t.Error("a format-2 restore result has null warnings")
	}

	// And a verify of the same point proves it.
	vo, err := svc.execRestore(ctx, testJobID, v2RestoreParams(t, store.prefix+*res.ManifestRef, nil, "",
		modeVerify, nil))
	if err != nil || !vo.res.Verified {
		t.Fatalf("verify: %+v %v", vo.res, err)
	}
}

func TestAFormat1RunStillWritesAFormat1Manifest(t *testing.T) {
	src := buildFixture(t)
	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil || res.State != stateSuccess {
		t.Fatal(err)
	}
	m1, m2 := readManifestDoc(t, store, *res.ManifestRef)
	if m1 == nil || m2 != nil {
		t.Fatal("a format-1 run did not write schema_version 1; the server that sent it cannot read v2")
	}
	if _, ok := res.DedupStats["manifest_pages"]; ok {
		t.Error("a format-1 result carries a format-2 stat")
	}
}

func TestAnOldCallerOfOpenManifestRefusesAV2Manifest(t *testing.T) {
	key, _ := decodeRepoKey(testKey)
	blob, err := sealJSON(key, &manifestV2{SchemaVersion: 2, Streams: []streamV2{}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := openManifest(key, blob); err == nil {
		t.Fatal("openManifest accepted a v2 document")
	}
	blob, _ = sealJSON(key, map[string]any{"schema_version": 3})
	_, _, err = openManifestDoc(key, blob)
	mustRefuse(t, err, "schema_version is 3")
}

func TestAPagedRestoreHonoursTheSelection(t *testing.T) {
	src := buildFixture(t)
	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	ctx := context.Background()
	o, err := svc.execRun(ctx, testJobID, v2RunParams(t, []string{src}, nil))
	if err != nil || o.res.State != stateSuccess {
		t.Fatal(err)
	}
	dest := filepath.Join(t.TempDir(), "r")
	sel := filepath.Join(src, "a", "b")
	ro, err := svc.execRestore(ctx, testJobID, v2RestoreParams(t, store.prefix+*o.res.ManifestRef,
		[]string{sel}, dest, modeRestore, nil))
	if err != nil || ro.res.State != stateSuccess {
		t.Fatalf("%+v %v", ro.res, err)
	}
	want := filepath.ToSlash(expectedDest(dest, sel))
	var deep bool
	for _, p := range listAll(t, dest) {
		full := filepath.ToSlash(filepath.Join(dest, p))
		if !strings.HasPrefix(full, want) && !strings.HasPrefix(want, full) {
			t.Errorf("%s restored outside the selection", p)
		}
		deep = deep || strings.HasSuffix(full, "deep.txt")
	}
	if !deep {
		t.Error("the selected subtree was not restored")
	}
}

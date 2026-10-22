package backup

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// THE TEST THAT PROVES THE FORMAT WORKS.
//
// Everything else in this package can be individually correct and the
// repository still unusable: a chunker that loses a byte at a buffer refill, a
// manifest that records a mode it never applies, a symlink written as the file
// it points at. The only assertion that catches all of those at once is a real
// tree, backed up and restored somewhere else, compared byte for byte —
// including permissions and symlinks.
//
// WHAT NEVER EXECUTED, stated plainly rather than left to be discovered: no
// volume snapshot is taken here. A Linux container has no VSS and no APFS, so
// the Snapshotter seam is filled with a passthrough that reads the live
// filesystem. Every other line of both capabilities — params, key handling,
// chunking, dedup, encryption, the manifest, the object layout, the whole
// restore path and all of its defences — is what runs below.

func TestARealTreeSurvivesBackupAndRestoreByteForByte(t *testing.T) {
	src := buildFixture(t)
	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	ctx := context.Background()

	// ---- backup -----------------------------------------------------------
	res, err := svc.run(ctx, testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil {
		t.Fatalf("backup.run refused: %v", err)
	}
	if res.State != stateSuccess {
		t.Fatalf("backup.run reported state=%s error=%v", res.State, deref(res.Error))
	}
	if res.ManifestRef == nil || *res.ManifestRef == "" {
		t.Fatal("backup.run reported success with no manifest_ref; ingest_backup_result would " +
			"record the run as failed and create no backup point")
	}
	if !store.closed {
		t.Error("the repository transport was not closed; a capability that holds a transport " +
			"between jobs is not dormant")
	}

	// The counters must be about this tree and not about nothing.
	wantBytes := int64(0)
	for _, f := range fixtureFiles() {
		if f.size >= 0 {
			wantBytes += int64(f.size)
		} else {
			wantBytes += int64(len(f.lit))
		}
	}
	if res.BytesProtected != wantBytes {
		t.Errorf("bytes_protected = %d, want %d (the sum of the fixture's file sizes)",
			res.BytesProtected, wantBytes)
	}
	if res.ChunkCount == 0 || res.BytesStored == 0 {
		t.Fatalf("chunk_count=%d bytes_stored=%d; the run stored nothing",
			res.ChunkCount, res.BytesStored)
	}
	// duplicate-of-large.bin is byte-identical to large-100k.bin, so every one
	// of its chunks must have deduped. Without this the round trip would pass
	// with dedup completely broken.
	deduped, _ := res.DedupStats["chunks_deduped"].(int64)
	if deduped == 0 {
		t.Errorf("chunks_deduped = 0, and the fixture contains two byte-identical 100 KB files; "+
			"dedup did nothing. dedup_stats=%v", res.DedupStats)
	}
	if len(store.overwritten) != 0 {
		t.Errorf("chunk objects were overwritten: %v. Spec §5 is store-once by id "+
			"(first-writer-wins)", store.overwritten)
	}
	if got, want := store.chunkCount(), int(res.ChunkCount); got != want {
		t.Errorf("the repository holds %d chunk objects and the result claims chunk_count=%d",
			got, want)
	}

	// ---- restore ----------------------------------------------------------
	dest := filepath.Join(t.TempDir(), "restored")
	rres, err := svc.restore(ctx, testJobID, restoreParamsJSON(t, store.prefix+*res.ManifestRef, []string{src}, dest, modeRestore))
	if err != nil {
		t.Fatalf("backup.restore refused: %v", err)
	}
	if rres.State != stateSuccess {
		t.Fatalf("backup.restore reported state=%s error=%v", rres.State, deref(rres.Error))
	}
	if len(rres.Mismatch) != 0 {
		t.Fatalf("backup.restore reported mismatches: %v", rres.Mismatch)
	}
	if rres.RestoredBytes != wantBytes {
		t.Errorf("restored_bytes = %d, want %d", rres.RestoredBytes, wantBytes)
	}
	if rres.Verified {
		t.Error("a mode=restore result reported verified=true. ingest_restore_result stamps " +
			"backup_points.restore_tested_at — the SOC 2 evidence that a point is provably " +
			"restorable — on exactly that flag, and a three-file operator restore is not proof")
	}

	// ---- compare ----------------------------------------------------------
	before := walkTree(t, src)
	restoredRoot := expectedDest(dest, src)
	after := walkTree(t, restoredRoot)

	if len(before) != len(after) {
		t.Errorf("the source tree has %d entries and the restored tree has %d",
			len(before), len(after))
	}
	for rel, want := range before {
		got, ok := after[rel]
		if !ok {
			t.Errorf("%s is missing from the restored tree", rel)
			continue
		}
		if got.kind != want.kind {
			t.Errorf("%s restored as %s, was %s", rel, got.kind, want.kind)
			continue
		}
		if got.kind != kindSymlink && got.mode != want.mode {
			t.Errorf("%s restored with mode %04o, was %04o", rel, got.mode, want.mode)
		}
		switch want.kind {
		case kindFile:
			if !bytes.Equal(got.content, want.content) {
				t.Errorf("%s restored with different contents (%d bytes vs %d)",
					rel, len(got.content), len(want.content))
			}
		case kindSymlink:
			if got.target != want.target {
				t.Errorf("%s restored pointing at %q, was %q", rel, got.target, want.target)
			}
		}
	}
	for rel := range after {
		if _, ok := before[rel]; !ok {
			t.Errorf("%s appeared in the restored tree and is not in the source", rel)
		}
	}
	t.Logf("round trip: %d entries, %d bytes protected, %d bytes stored, %d chunks, "+
		"dedup_stats=%v", len(before), res.BytesProtected, res.BytesStored, res.ChunkCount,
		res.DedupStats)
}

func TestTheEmptyFileAndTheDeepPathAndTheNonASCIINameSurviveIndividually(t *testing.T) {
	// The tree-wide comparison above would pass if a whole CLASS of entry were
	// dropped from BOTH sides — e.g. if the walker skipped empty files and the
	// comparison therefore never looked for one. These name the entries that
	// have to be there.
	//
	// FALSIFYING OBSERVATION: an entry the fixture creates that the manifest
	// does not contain.
	src := buildFixture(t)
	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil {
		t.Fatalf("backup.run refused: %v", err)
	}

	m := readManifest(t, store, store.prefix+*res.ManifestRef)
	byPath := map[string]manifestEntry{}
	for _, e := range filetreeEntries(m) {
		byPath[e.Path] = e
	}

	for _, c := range []struct {
		rel  string
		kind string
		size int64
		why  string
	}{
		{"empty.txt", kindFile, 0, "an empty file has zero chunks and must still be an entry"},
		{"one-byte.bin", kindFile, 1, "one byte is far below min"},
		{"under-min-511.bin", kindFile, 511, "one below min"},
		{"exactly-min-512.bin", kindFile, 512, "exactly min"},
		{"exactly-max-4096.bin", kindFile, 4096, "exactly max"},
		{"over-max-4097.bin", kindFile, 4097, "one over max"},
		{"naïve-café-日本語.txt", kindFile, 39, "a non-ASCII filename (39 UTF-8 bytes of contents)"},
		{"a/b/c/d/e/f/g/h/i/deep.txt", kindFile, 17, "nine directory levels"},
		{"empty-dir", kindDir, 0, "a directory with nothing in it"},
		{"rel-link", kindSymlink, 0, "a relative symlink"},
		{"dangling-link", kindSymlink, 0, "a symlink whose target does not exist"},
	} {
		e, ok := byPath[toManifestPath(filepath.Join(src, filepath.FromSlash(c.rel)))]
		if !ok {
			t.Errorf("the manifest has no entry for %s (%s)", c.rel, c.why)
			continue
		}
		if e.Kind != c.kind {
			t.Errorf("%s is kind %q in the manifest, want %q", c.rel, e.Kind, c.kind)
		}
		if c.kind == kindFile && e.Size != c.size {
			t.Errorf("%s has size %d in the manifest, want %d", c.rel, e.Size, c.size)
		}
		if c.kind == kindFile && c.size == 0 && len(e.ChunkIDs) != 0 {
			t.Errorf("the empty file has %d chunk ids; an empty file has zero chunks, not one "+
				"empty chunk", len(e.ChunkIDs))
		}
		if c.kind == kindFile && c.size > 4096 && len(e.ChunkIDs) < 2 {
			t.Errorf("%s is %d bytes with max=%d and has %d chunk ids; the forced max boundary "+
				"did not fire", c.rel, c.size, testDedup().Max, len(e.ChunkIDs))
		}
	}
}

func TestAFileWithNoTrailingNewlineComesBackWithNoTrailingNewline(t *testing.T) {
	// FIXTURE-BLINDNESS GUARD, stated as its own test because it is the one
	// mutation a whole-tree comparison would hide if BOTH sides were written by
	// the same buggy helper.
	//
	// THE MUTATION: the restored bytes, with a "\n" appended. Asserted to differ
	// from the original before the comparison runs.
	src := buildFixture(t)
	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	ctx := context.Background()

	res, err := svc.run(ctx, testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil {
		t.Fatalf("backup.run refused: %v", err)
	}
	dest := filepath.Join(t.TempDir(), "restored")
	if _, err := svc.restore(ctx, testJobID, restoreParamsJSON(t, store.prefix+*res.ManifestRef, []string{src}, dest, modeRestore)); err != nil {
		t.Fatalf("backup.restore refused: %v", err)
	}

	original, err := os.ReadFile(filepath.Join(src, "no-trailing-newline.txt"))
	if err != nil {
		t.Fatal(err)
	}
	restored, err := os.ReadFile(filepath.Join(expectedDest(dest, src), "no-trailing-newline.txt"))
	if err != nil {
		t.Fatal(err)
	}

	mutated := append(bytes.Clone(original), '\n')
	if bytes.Equal(mutated, original) {
		t.Fatal("the mutation did not apply")
	}
	if bytes.Equal(restored, mutated) {
		t.Fatalf("the restored file is %q — a trailing newline was added that the source does "+
			"not have", restored)
	}
	if !bytes.Equal(restored, original) {
		t.Fatalf("restored %q, source %q", restored, original)
	}
	if bytes.HasSuffix(restored, []byte("\n")) {
		t.Fatalf("the restored file ends in a newline and the source does not: %q", restored)
	}
}

func TestASelectionRestoresThatSubtreeAndNothingElse(t *testing.T) {
	// A selection that restored everything would pass every content comparison
	// above and be a serious data-handling bug: an operator restoring one
	// directory would silently overwrite a whole tree.
	//
	// FALSIFYING OBSERVATION: a restored path outside the selected subtree.
	src := buildFixture(t)
	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	ctx := context.Background()

	res, err := svc.run(ctx, testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil {
		t.Fatalf("backup.run refused: %v", err)
	}
	dest := filepath.Join(t.TempDir(), "restored")
	sel := toManifestPath(filepath.Join(src, "a/b/c"))
	rres, err := svc.restore(ctx, testJobID, restoreParamsJSON(t, store.prefix+*res.ManifestRef, []string{sel}, dest, modeRestore))
	if err != nil {
		t.Fatalf("backup.restore refused: %v", err)
	}
	if rres.State != stateSuccess {
		t.Fatalf("state=%s error=%v", rres.State, deref(rres.Error))
	}

	got := listAll(t, dest)
	for _, p := range got {
		if strings.HasSuffix(p, "empty.txt") || strings.HasSuffix(p, "large-100k.bin") {
			t.Errorf("%s was restored and is outside the selection %s", p, sel)
		}
	}
	found := false
	for _, p := range got {
		if strings.HasSuffix(p, "deep.txt") {
			found = true
		}
	}
	if !found {
		t.Errorf("the selected subtree's file was not restored; got %v", got)
	}
	// A selection of `/x/a/b/c` must NOT also select a sibling whose name
	// merely starts with the same characters.
	if selects([]string{"/x/a/b/c"}, "/x/a/b/c-2024-salaries.xlsx") {
		t.Error("a selection of a directory also selected a sibling file whose name starts with " +
			"the same characters; the prefix test is missing its separator")
	}
}

func TestAVerifyRestoresIntoScratchProvesThePointAndLeavesNothingBehind(t *testing.T) {
	// §9's proven-restore. ingest_restore_result stamps
	// backup_points.restore_tested_at on `mode=verify` + `verified=true` +
	// empty mismatch, and that timestamp is SOC 2 / BCP evidence — so both the
	// flag and the cleanup have to be real.
	//
	// FALSIFYING OBSERVATION: verified=false on an intact repository, or a
	// scratch directory that still exists afterwards (a full second copy of
	// every protected byte, left on the endpoint, in a location no policy
	// describes).
	src := buildFixture(t)
	store := newFakeStore(testStorageRef)
	scratchParent := t.TempDir()
	svc := New(Options{
		OpenStore: func(context.Context, Repository) (ObjectStore, error) { return store, nil },
		OpenSnapshot: func(context.Context, string, []string) (Snapshot, error) {
			return passthroughSnapshot{}, nil
		},
		QueryChunks:   newFakeDedup(testStorageRef).answer,
		ScratchParent: scratchParent,
		Logger:        quiet(),
	})
	ctx := context.Background()

	res, err := svc.run(ctx, testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil {
		t.Fatalf("backup.run refused: %v", err)
	}

	// api.py::verify_point sends selection=[] and target={}: exactly this.
	rres, err := svc.restore(ctx, testJobID, restoreParamsJSON(t, store.prefix+*res.ManifestRef, nil, "", modeVerify))
	if err != nil {
		t.Fatalf("backup.restore refused: %v", err)
	}
	if rres.State != stateSuccess || !rres.Verified || len(rres.Mismatch) != 0 {
		t.Fatalf("verify reported state=%s verified=%v mismatch=%v error=%v",
			rres.State, rres.Verified, rres.Mismatch, deref(rres.Error))
	}
	if rres.RestoredBytes == 0 {
		t.Error("a verify with selection=[] restored zero bytes; an empty selection in verify " +
			"mode means the whole point (api.py::verify_point's docstring)")
	}
	if left := listAll(t, scratchParent); len(left) != 0 {
		t.Errorf("the verify left %d paths in the scratch parent: %v", len(left), left)
	}
}

func TestAVerifyFailsWhenAChunkInStorageHasBeenAlteredAndNamesThePath(t *testing.T) {
	// The verify has to be able to say NO, and it has to say which path. A
	// verify that returns verified=true unconditionally passes every test above
	// and stamps restore_tested_at on an unrestorable backup point — the exact
	// failure proven-restore exists to prevent.
	//
	// THE MUTATION: one byte of one stored ciphertext object, flipped. Asserted
	// changed before the verify runs. AES-GCM authentication catches it, which
	// is the point: the object no longer authenticates under the repository key.
	//
	// FALSIFYING OBSERVATION: verified=true after the mutation, or an empty
	// mismatch list.
	src := buildFixture(t)
	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	ctx := context.Background()

	res, err := svc.run(ctx, testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil {
		t.Fatalf("backup.run refused: %v", err)
	}

	m := readManifest(t, store, store.prefix+*res.ManifestRef)
	var victimPath, victimChunk string
	for _, e := range filetreeEntries(m) {
		if e.Kind == kindFile && len(e.ChunkIDs) > 0 && strings.HasSuffix(e.Path, "large-100k.bin") {
			victimPath, victimChunk = e.Path, e.ChunkIDs[0]
			break
		}
	}
	if victimChunk == "" {
		t.Fatal("the fixture produced no multi-chunk file; this test's premise is stale")
	}

	suffix := chunkSuffix(victimChunk)
	store.mu.Lock()
	before := bytes.Clone(store.objects[suffix])
	store.objects[suffix][nonceLen+5] ^= 0x01
	after := bytes.Clone(store.objects[suffix])
	store.mu.Unlock()
	if bytes.Equal(before, after) {
		t.Fatal("the mutation did not apply; this test would pass while measuring nothing")
	}

	rres, err := svc.restore(ctx, testJobID, restoreParamsJSON(t, store.prefix+*res.ManifestRef, nil, "", modeVerify))
	if err != nil {
		t.Fatalf("backup.restore refused: %v", err)
	}
	if rres.Verified {
		t.Fatal("verified=true after a stored chunk was altered. ingest_restore_result would " +
			"stamp restore_tested_at on a backup point that cannot be restored")
	}
	if len(rres.Mismatch) == 0 {
		t.Fatal("the verify reported no mismatch after a stored chunk was altered")
	}
	found := false
	for _, p := range rres.Mismatch {
		if p == victimPath {
			found = true
		}
	}
	if !found {
		t.Errorf("mismatch = %v, and the altered chunk belongs to %s", rres.Mismatch, victimPath)
	}
}

func TestTheRepositoryHoldsNoPlaintextOfAnyProtectedFile(t *testing.T) {
	// Spec §5/§7: chunks and manifests are AES-256-GCM under the repository
	// key. An engine that forgot to encrypt would round-trip perfectly and pass
	// every other test in this file.
	//
	// FALSIFYING OBSERVATION: a distinctive plaintext string, or a source path,
	// found verbatim in any stored object.
	src := buildFixture(t)
	marker := []byte("CANARY-a3f1-customer-payroll-2026")
	if err := os.WriteFile(filepath.Join(src, "canary.txt"), marker, 0o600); err != nil {
		t.Fatal(err)
	}
	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	if _, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil)); err != nil {
		t.Fatalf("backup.run refused: %v", err)
	}

	blobs := store.blobs()
	if len(blobs) == 0 {
		t.Fatal("nothing was stored; this test would pass while measuring nothing")
	}
	for _, b := range blobs {
		if bytes.Contains(b, marker) {
			t.Fatal("a stored repository object contains a protected file's plaintext")
		}
		if bytes.Contains(b, []byte("canary.txt")) {
			t.Fatal("a stored repository object contains a source path in the clear; the " +
				"manifest is supposed to be encrypted too")
		}
	}
	// And the guard is not vacuous: the same search DOES find the marker in the
	// bytes before they are sealed.
	if !bytes.Contains(marker, []byte("CANARY")) {
		t.Fatal("the marker search itself is broken")
	}
}

// ---------------------------------------------------------------------------

func readManifest(t *testing.T, store *fakeStore, ref string) *backupManifest {
	t.Helper()
	key, err := decodeRepoKey(testKey)
	if err != nil {
		t.Fatal(err)
	}
	defer key.zero()
	blob, err := store.GetRef(context.Background(), ref)
	if err != nil {
		t.Fatal(err)
	}
	m, err := openManifest(key, blob)
	if err != nil {
		t.Fatal(err)
	}
	return m
}

func deref(s *string) string {
	if s == nil {
		return "<nil>"
	}
	return *s
}

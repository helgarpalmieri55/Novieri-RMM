package backup

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Restore never destroys the original before its replacement is complete
// (spec-server-backup.md §7.1, D9), and the conflict modes do what they say.

// restoredOnce backs up the fixture and restores it once into dest, so dest
// holds "originals" a second restore can be pointed at.
func restoredOnce(t *testing.T) (svc *Service, store *fakeStore, src, dest, ref string) {
	t.Helper()
	src = buildFixture(t)
	store = newFakeStore(testStorageRef)
	svc = newTestService(store, quiet())
	res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil || res.State != stateSuccess {
		t.Fatalf("%v %+v", err, res)
	}
	ref = store.prefix + *res.ManifestRef
	dest = filepath.Join(t.TempDir(), "in-place")
	r, err := svc.restore(context.Background(), testJobID, restoreParamsJSON(t, ref, []string{src}, dest,
		modeRestore))
	if err != nil || r.State != stateSuccess {
		t.Fatalf("%v %+v", err, r)
	}
	return svc, store, src, dest, ref
}

func leftovers(t *testing.T, dir string) []string {
	t.Helper()
	var out []string
	for _, p := range listAll(t, dir) {
		if strings.Contains(filepath.Base(p), ".itms-restore-") {
			out = append(out, p)
		}
	}
	return out
}

func TestAnInPlaceRestoreThatFailsMidFileLeavesTheOriginalIntact(t *testing.T) {
	svc, store, src, dest, ref := restoredOnce(t)
	// duplicate-of-large.bin is the first multi-chunk file in restore order.
	name := "duplicate-of-large.bin"
	target := filepath.Join(expectedDest(dest, src), name)
	original := []byte("THE ORIGINAL, WHICH A FAILED RESTORE MUST NOT TOUCH")
	if err := os.WriteFile(target, original, 0o644); err != nil {
		t.Fatal(err)
	}
	m := readManifest(t, store, ref)
	var second string
	for _, e := range filetreeEntries(m) {
		if strings.HasSuffix(e.Path, "/"+name) {
			second = e.ChunkIDs[1]
		}
	}
	if second == "" {
		t.Fatal("the fixture's file has fewer than two chunks")
	}
	// Fail the transport on that file's SECOND chunk: its first is already
	// written to the new copy when the failure lands.
	store.failGet = func(suffix string) error {
		if suffix == chunkSuffix(second) {
			return errors.New("connection reset by peer")
		}
		return nil
	}
	r, err := svc.restore(context.Background(), testJobID, restoreParamsJSON(t, ref, []string{src}, dest,
		modeRestore))
	if err != nil {
		t.Fatal(err)
	}
	if r.State != stateFailed {
		t.Fatalf("a restore whose transport failed reported %s", r.State)
	}
	got, err := os.ReadFile(target)
	if err != nil {
		t.Fatalf("the original is gone after a failed restore: %v", err)
	}
	if string(got) != string(original) {
		t.Fatalf("the original holds %d bytes of something else after a failed restore", len(got))
	}
	if l := leftovers(t, dest); len(l) != 0 {
		t.Errorf("temporary files left behind: %v", l)
	}
}

func TestATamperedChunkLeavesTheOriginalAndIsReportedByPath(t *testing.T) {
	svc, store, src, dest, ref := restoredOnce(t)
	target := filepath.Join(expectedDest(dest, src), "exactly-avg-1024.bin")
	original := []byte("the original")
	if err := os.WriteFile(target, original, 0o644); err != nil {
		t.Fatal(err)
	}
	// Swap every chunk object's ciphertext for another chunk's: each still
	// decrypts, none hashes to its id.
	store.mu.Lock()
	var keys []string
	for k := range store.objects {
		if strings.HasPrefix(k, "chunks/") {
			keys = append(keys, k)
		}
	}
	for i := range keys {
		j := (i + 1) % len(keys)
		store.objects[keys[i]], store.objects[keys[j]] = store.objects[keys[j]], store.objects[keys[i]]
	}
	store.mu.Unlock()

	r, err := svc.restore(context.Background(), testJobID, restoreParamsJSON(t, ref, []string{src}, dest,
		modeRestore))
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Mismatch) == 0 {
		t.Fatal("tampered chunks restored without a mismatch")
	}
	if got, _ := os.ReadFile(target); string(got) != string(original) {
		t.Fatalf("the original was replaced by unverified bytes: %q", got)
	}
	if l := leftovers(t, dest); len(l) != 0 {
		t.Errorf("temporary files left behind: %v", l)
	}
}

func TestConflictSkipKeepsTheExistingFileAndRenamePutsTheCopyBeside(t *testing.T) {
	svc, _, src, dest, ref := restoredOnce(t)
	target := filepath.Join(expectedDest(dest, src), "one-byte.bin")
	mine := []byte("edited since the backup")
	if err := os.WriteFile(target, mine, 0o644); err != nil {
		t.Fatal(err)
	}
	run := func(conflict string) *restoreResult {
		t.Helper()
		o, err := svc.execRestore(context.Background(), testJobID, v2RestoreParams(t, ref,
			[]string{filepath.Join(src, "one-byte.bin")}, dest, modeRestore, map[string]any{"conflict": conflict}))
		if err != nil || o.res.State != stateSuccess || len(o.res.Mismatch) != 0 {
			t.Fatalf("%s: %v %+v", conflict, err, o.res)
		}
		return o.res
	}

	run(conflictSkip)
	if got, _ := os.ReadFile(target); string(got) != string(mine) {
		t.Fatalf("skip replaced the file: %q", got)
	}

	run(conflictRename)
	if got, _ := os.ReadFile(target); string(got) != string(mine) {
		t.Fatalf("rename replaced the file: %q", got)
	}
	matches, _ := filepath.Glob(target + ".restored-*")
	if len(matches) != 1 {
		t.Fatalf("rename produced %v", matches)
	}
	want, _ := os.ReadFile(filepath.Join(src, "one-byte.bin"))
	if got, _ := os.ReadFile(matches[0]); string(got) != string(want) {
		t.Fatalf("the renamed copy holds %q", got)
	}
	run(conflictRename)
	if again, _ := filepath.Glob(target + ".restored-*"); len(again) != 2 {
		t.Fatalf("a second rename restore overwrote the first copy: %v", again)
	}

	run(conflictOverwrite)
	if got, _ := os.ReadFile(target); string(got) != string(want) {
		t.Fatalf("overwrite left %q", got)
	}
}

func TestAnOverwriteOfAReadOnlyFileSucceeds(t *testing.T) {
	// Windows refuses to rename over a read-only file; replaceFile clears the
	// attribute on the file it is about to replace, and only then.
	svc, _, src, dest, ref := restoredOnce(t)
	target := filepath.Join(expectedDest(dest, src), "no-trailing-newline.txt")
	if err := os.WriteFile(target, []byte("old"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(target, 0o444); err != nil {
		t.Fatal(err)
	}
	r, err := svc.restore(context.Background(), testJobID, restoreParamsJSON(t, ref,
		[]string{filepath.Join(src, "no-trailing-newline.txt")}, dest, modeRestore))
	if err != nil || r.State != stateSuccess || len(r.Mismatch) != 0 {
		t.Fatalf("%v %+v", err, r)
	}
	if got, _ := os.ReadFile(target); string(got) != "the last line has no newline" {
		t.Fatalf("got %q", got)
	}
}

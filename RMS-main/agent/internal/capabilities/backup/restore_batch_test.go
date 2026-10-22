package backup

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

// expectingStore records the order restore announces, and every Get. It
// embeds the package's real fakeStore rather than adding a second fake-store
// construction path: every byte it serves and every write grant it checks is
// exactly what fakeStore already enforces, and this type ADDS ONLY the
// bookkeeping the fetch-order assertion needs.
type expectingStore struct {
	*fakeStore
	expected []string
	got      []string

	// Everything runRestore needs to drive an actual restore against this
	// store, so newExpectingStore's caller does not have to reassemble it.
	svc       *Service
	manifest  string
	selection []string
	dest      string
}

func (e *expectingStore) Expect(suffixes []string) { e.expected = append([]string(nil), suffixes...) }

func (e *expectingStore) Get(ctx context.Context, suffix string) ([]byte, error) {
	e.got = append(e.got, suffix)
	return e.fakeStore.Get(ctx, suffix)
}

// newExpectingStore builds a fixture backup whose single large file chunks
// into at least minChunks distinct chunks at the package's test dedup
// parameters, runs a real backup.run over it through the package's existing
// fakeStore and newTestService, and returns an expectingStore wrapping the
// result together with everything runRestore needs to restore it.
//
// minChunks is chosen by the caller to be large enough that a window of 1 --
// what a store gets when restore never calls Expect -- would be obviously,
// not marginally, wrong: spec §6.1 rejects a URL per chunk outright once the
// count reaches the low hundreds, let alone six figures.
func newExpectingStore(t *testing.T, minChunks int) *expectingStore {
	t.Helper()

	root := t.TempDir()
	src := filepath.Join(root, "src")
	if err := os.MkdirAll(src, 0o755); err != nil {
		t.Fatal(err)
	}

	// At testDedup()'s avg of 1024 bytes, the realised mean chunk size runs
	// noticeably above the nominal average (the min-512 floor skews it), so
	// the multiplier here is generous rather than tight. Pseudo-random
	// content, not repetitive: a rolling hash over a repeating pattern lands
	// boundaries at pathologically regular intervals, which would make the
	// chunk count an artifact of the fixture rather than of the chunker.
	size := (minChunks*2 + 400) * 1024
	content := make([]byte, size)
	fill(content, 0xC0FFEE)
	if err := os.WriteFile(filepath.Join(src, "big.bin"), content, 0o644); err != nil {
		t.Fatal(err)
	}

	base := newFakeStore(testStorageRef)
	runSvc := newTestService(base, quiet())
	res, err := runSvc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil {
		t.Fatalf("backup.run refused: %v", err)
	}
	if res.State != stateSuccess {
		t.Fatalf("backup.run failed: %v", deref(res.Error))
	}
	if got := base.chunkCount(); got < minChunks {
		t.Fatalf("the fixture produced %d chunks, want at least %d -- widen the fixture file", got, minChunks)
	}

	st := &expectingStore{fakeStore: base}
	st.svc = New(Options{
		OpenStore: func(context.Context, Repository) (ObjectStore, error) { return st, nil },
		Logger:    quiet(),
	})
	st.manifest = base.prefix + *res.ManifestRef
	st.selection = []string{toManifestPath(src)}
	st.dest = filepath.Join(root, "restored")
	return st
}

// runRestore drives a real backup.restore over the fixture newExpectingStore
// built, through the same store it returned -- swapping stores here would
// prove nothing about what restore.go actually announces to.
func runRestore(t *testing.T, st *expectingStore) {
	t.Helper()
	// st.svc was wired (in newExpectingStore) with OpenStore returning st
	// itself, so restoring through st.svc exercises exactly the store whose
	// expected/got this test inspects.
	res, err := st.svc.restore(context.Background(), testJobID,
		restoreParamsJSON(t, st.manifest, st.selection, st.dest, modeRestore))
	if err != nil {
		t.Fatalf("backup.restore refused: %v", err)
	}
	if res.State != stateSuccess {
		t.Fatalf("backup.restore failed: %v", deref(res.Error))
	}
}

func TestRestoreAnnouncesEveryChunkItIsAboutToFetch(t *testing.T) {
	// Without the announcement the store's window is 1, which is one signed
	// URL per chunk -- the six-figure round-trip problem spec §6.1 rejected
	// outright.
	st := newExpectingStore(t, 600) // a manifest whose selection spans 600+ chunks
	runRestore(t, st)

	if len(st.expected) == 0 {
		t.Fatal("restore fetched chunks without announcing them; the store cannot batch")
	}
	if len(st.got) == 0 {
		t.Fatal("the test fetched nothing, so it proves nothing")
	}
	// Every fetched chunk must have been announced, in the order fetched.
	announced := map[string]int{}
	for i, s := range st.expected {
		announced[s] = i
	}
	last := -1
	for _, s := range st.got {
		i, ok := announced[s]
		if !ok {
			t.Fatalf("restore fetched %q, which it never announced", s)
		}
		if i < last {
			t.Fatalf("restore fetched %q out of the announced order", s)
		}
		last = i
	}
	// DISTINCTNESS: the announced order names each suffix once. A collector
	// that appended every chunk id on every entry, duplicates included, would
	// still pass the ordering check above (a duplicate never appears "out of
	// order" relative to itself) but would defeat the whole point of a
	// look-ahead window by making it slide past repeats it should already
	// have satisfied.
	seenAnnounced := map[string]int{}
	for _, s := range st.expected {
		seenAnnounced[s]++
	}
	for s, n := range seenAnnounced {
		if n > 1 {
			t.Fatalf("the announced order names %q %d times; it must be distinct", s, n)
		}
	}
}

// resealFixtureManifest fetches st's current (real, backup.run-produced)
// manifest, decrypts it, lets mutate rewrite the decrypted struct in place,
// reseals it and stores the result as a NEW manifest object, then points
// st.manifest at that object.
//
// This builds on newExpectingStore/expectingStore rather than adding a
// second harness: the store, its real chunks, and the encryption key are all
// the fixture's, so a mutated manifest can sit a forged or malformed entry
// directly next to genuinely-uploaded chunks whose ids are real -- which is
// what lets a test assert a POSITIVE ("this real id IS announced") alongside
// the negative one, instead of only proving something absent from an empty
// announcement.
func resealFixtureManifest(t *testing.T, st *expectingStore, mutate func(m *backupManifest)) {
	t.Helper()
	key, err := decodeRepoKey(testKey)
	if err != nil {
		t.Fatalf("could not decode the fixture's repo key: %v", err)
	}
	defer key.zero()

	blob, err := st.fakeStore.GetRef(context.Background(), st.manifest)
	if err != nil {
		t.Fatalf("could not fetch the fixture manifest to mutate it: %v", err)
	}
	m, err := openManifest(key, blob)
	if err != nil {
		t.Fatalf("could not open the fixture manifest: %v", err)
	}
	mutate(m)

	sealed, err := sealManifest(key, m)
	if err != nil {
		t.Fatalf("could not reseal the mutated manifest: %v", err)
	}
	suffix, err := st.fakeStore.Put(context.Background(), testGrant(), "manifests/mutated", sealed)
	if err != nil {
		t.Fatalf("could not store the mutated manifest: %v", err)
	}
	st.manifest = st.fakeStore.prefix + suffix
}

// TestRestoreDoesNotAnnounceAForgedNonFileEntrysChunks plants the first named
// divergence: the collector's gate must be positive (`e.Kind == kindFile`),
// matching r.entry's switch, not negative (`e.Kind == kindSymlink`). r.entry
// never reaches ChunkIDs for a kindDir entry -- it returns after
// ensureDirUnderRoot -- so a manifest carrying a `dir` entry with a populated
// chunk_ids list must have those ids announced to nobody.
func TestRestoreDoesNotAnnounceAForgedNonFileEntrysChunks(t *testing.T) {
	st := newExpectingStore(t, 600)

	// 64 lowercase hex characters, satisfying validChunkID, but not a chunk
	// this fixture (or any fixture) ever uploaded -- if it is announced at
	// all, the collector treated a non-file entry's chunk_ids as real.
	const forgedID = "abababababababababababababababababababababababababababababababab"

	var legitID string
	resealFixtureManifest(t, st, func(m *backupManifest) {
		entries := m.Streams[0].Entries
		var dirPath string
		for i := range entries {
			switch entries[i].Kind {
			case kindFile:
				if len(entries[i].ChunkIDs) > 0 {
					legitID = entries[i].ChunkIDs[0]
				}
			case kindDir:
				dirPath = entries[i].Path
			}
		}
		if dirPath == "" {
			t.Fatal("the fixture produced no dir entry to anchor the forged one under -- this test proves nothing")
		}
		// A forged directory entry. newEntry (the manifest's own writer) never
		// populates chunk_ids on anything but a file; nothing on the READ side
		// enforces that, which is exactly what this entry is here to catch.
		m.Streams[0].Entries = append(entries, manifestEntry{
			Path:     dirPath + "/forged-dir-carrying-chunk-ids",
			Kind:     kindDir,
			Mode:     0o755,
			ChunkIDs: []string{forgedID},
			Target:   nil,
		})
	})
	if legitID == "" {
		t.Fatal("the fixture produced no file entry with chunks -- this test proves nothing")
	}

	runRestore(t, st)

	for _, s := range st.expected {
		if s == chunkSuffix(forgedID) {
			t.Fatalf("a forged `dir` entry's chunk id %q was announced; r.entry would never fetch it", s)
		}
	}
	// POSITIVE: the legitimate file's own leading chunk id must still be
	// announced. Without this, a fixture that silently built no file entry
	// (or an st.expected that is simply empty) would pass the check above
	// vacuously.
	found := false
	for _, s := range st.expected {
		if s == chunkSuffix(legitID) {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("the legitimate file's own leading chunk id %q was never announced", chunkSuffix(legitID))
	}
}

// TestRestoreStopsAnnouncingAtTheFirstInvalidChunkID plants the second named
// divergence: on an invalid chunk id the collector must stop collecting for
// that entry, matching r.stream, which returns (aborting the whole entry) on
// the first invalid id rather than skipping past it. For
// ChunkIDs = [valid, invalid, valid], the walk fetches only the first valid
// id; the id after the invalid one is never reached.
func TestRestoreStopsAnnouncingAtTheFirstInvalidChunkID(t *testing.T) {
	st := newExpectingStore(t, 600)

	var leadingID, trailingID string
	resealFixtureManifest(t, st, func(m *backupManifest) {
		entries := m.Streams[0].Entries
		for i := range entries {
			if entries[i].Kind != kindFile {
				continue
			}
			if len(entries[i].ChunkIDs) < 2 {
				t.Fatal("the fixture's file entry has fewer than two chunks -- this test proves nothing")
			}
			leadingID = entries[i].ChunkIDs[0]
			trailingID = entries[i].ChunkIDs[1]
			entries[i].ChunkIDs = []string{leadingID, "not-a-valid-chunk-id", trailingID}
		}
	})
	if leadingID == "" || trailingID == "" {
		t.Fatal("the fixture produced no file entry to mutate -- this test proves nothing")
	}

	runRestore(t, st)

	for _, s := range st.expected {
		if s == chunkSuffix(trailingID) {
			t.Fatalf("a chunk id after an invalid one was announced: %q; r.stream aborts the entry "+
				"at the invalid id and never reaches it", s)
		}
	}
	// POSITIVE: the leading, valid id BEFORE the invalid one must still be
	// announced, so a fixture that silently mutated nothing (or produced an
	// empty st.expected) cannot pass the check above vacuously.
	found := false
	for _, s := range st.expected {
		if s == chunkSuffix(leadingID) {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("the leading valid chunk id %q was never announced", chunkSuffix(leadingID))
	}
}

func TestRestorePassesItsJobIdToTheStore(t *testing.T) {
	// A read query names the job that authorizes it; an empty job id is
	// refused by the control plane in silence, which looks like a hang.
	var got Repository
	svc := New(Options{
		OpenStore: func(_ context.Context, repo Repository) (ObjectStore, error) {
			got = repo
			return newFakeStore(testStorageRef), nil
		},
		Logger: quiet(),
	})
	ref := "s3://test-bucket/" + testStorageRef + "/manifests/does-not-exist"
	_, _ = svc.restore(context.Background(), "job-42", restoreParamsJSON(t, ref, nil, "", modeVerify))
	if got.JobID != "job-42" {
		t.Fatalf("the store was opened with JobID %q, want job-42", got.JobID)
	}
}

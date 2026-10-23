package backup

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/budget"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// THE DEDUP EXCHANGE, MEASURED RATHER THAN DESCRIBED.
//
// Every fixture here VARIES what the control plane already holds. A test in
// which every chunk is new, or every chunk is held, passes against an
// implementation that uploads everything or uploads nothing — this project's
// defining defect — so each assertion below names WHICH ids were uploaded and
// WHICH were skipped, not how many.

// patternBytes builds content-defined-chunkable data: pseudo-random enough
// that the rolling hash finds boundaries, and DETERMINISTIC so two calls with
// the same length give the same chunk ids. `fill` is budget_test.go's, so this
// content straddles the same boundaries the fixture tree does.
func patternBytes(n int) []byte {
	b := make([]byte, n)
	const block = 1 << 16
	for off := 0; off < n; off += block {
		end := off + block
		if end > n {
			end = n
		}
		fill(b[off:end], uint64(off))
	}
	return b
}

// uploadedChunkIDs is what actually landed in the repository, as chunk ids.
func uploadedChunkIDs(store *fakeStore) []string {
	store.mu.Lock()
	defer store.mu.Unlock()
	out := []string{}
	for k := range store.objects {
		if i := strings.LastIndex(k, "/"); strings.HasPrefix(k, "chunks/") && i >= 0 {
			out = append(out, k[i+1:])
		}
	}
	sort.Strings(out)
	return out
}

// chunkIDsOf hashes `data` with the run's chunker parameters, giving the ids a
// run over that exact content will produce. A test can then pre-hold SOME of
// them.
func chunkIDsOf(t *testing.T, data []byte) []string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "f.bin")
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	c := newChunker(testDedup())
	c.reset(f)
	h := newBlake3()
	var out []string
	for {
		chunk, err := c.next()
		if err != nil {
			break
		}
		out = append(out, chunkID(h, chunk))
	}
	return out
}

// ---------------------------------------------------------------------------
// The partition
// ---------------------------------------------------------------------------

func TestOnlyTheChunksTheControlPlaneCallsNewAreUploaded(t *testing.T) {
	// FALSIFYING OBSERVATION: an id the control plane said it already held
	// arriving in the store, or an id it called new NOT arriving.
	//
	// The fixture pre-holds every OTHER chunk of a multi-chunk file, so an
	// implementation that uploaded all of them and one that uploaded none of
	// them both produce a different set from the one asserted.
	dir := t.TempDir()
	data := patternBytes(60_000)
	src := filepath.Join(dir, "f.bin")
	if err := os.WriteFile(src, data, 0o644); err != nil {
		t.Fatal(err)
	}

	all := chunkIDsOf(t, data)
	if len(all) < 6 {
		t.Fatalf("the fixture produced %d chunks; it needs enough to partition", len(all))
	}
	var held, want []string
	for i, id := range all {
		if i%2 == 0 {
			held = append(held, id)
		} else {
			want = append(want, id)
		}
	}

	store := newFakeStore(testStorageRef)
	svc, dedup := newTestServiceWithDedup(store, quiet())
	dedup.hold(held...)

	res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil || res.State != stateSuccess {
		t.Fatalf("backup.run: %v %+v", err, res)
	}

	sort.Strings(want)
	if got := uploadedChunkIDs(store); !equalStrings(got, want) {
		t.Fatalf("uploaded %v; the control plane called exactly %v new and already held %v",
			got, want, held)
	}
	// And the counters agree with the partition, so a run cannot report a dedup
	// ratio its uploads contradict.
	up, _ := res.DedupStats["chunks_uploaded"].(int64)
	dd, _ := res.DedupStats["chunks_deduped"].(int64)
	if int(up) != len(want) || int(dd) != len(held) {
		t.Errorf("reported %d uploaded / %d deduped; the partition was %d / %d",
			up, dd, len(want), len(held))
	}
}

func TestEveryUploadedChunkIsReportedBackSoTheNextRunDeduplicates(t *testing.T) {
	// The other half of the exchange. Without `stored`, the control plane never
	// learns what was written and EVERY run re-uploads EVERY chunk — a bug that
	// breaks nothing, fails no test that only checks the first run, and doubles
	// a customer's storage bill.
	//
	// FALSIFYING OBSERVATION: a second run over unchanged data uploading
	// anything at all.
	dir := t.TempDir()
	data := patternBytes(40_000)
	src := filepath.Join(dir, "f.bin")
	if err := os.WriteFile(src, data, 0o644); err != nil {
		t.Fatal(err)
	}

	store := newFakeStore(testStorageRef)
	svc, dedup := newTestServiceWithDedup(store, quiet())
	raw := runParamsJSON(t, []string{src}, nil)

	first, err := svc.run(context.Background(), testJobID, raw)
	if err != nil || first.State != stateSuccess {
		t.Fatalf("first run: %v %+v", err, first)
	}
	uploaded := uploadedChunkIDs(store)
	if len(uploaded) == 0 {
		t.Fatal("the first run uploaded nothing; the fixture proves nothing")
	}
	if got := dedup.held(); !equalStrings(got, uploaded) {
		t.Fatalf("the control plane was told %v was stored; %v actually was", got, uploaded)
	}

	store2 := newFakeStore(testStorageRef)
	svc2 := New(Options{
		OpenStore:    func(context.Context, Repository) (ObjectStore, error) { return store2, nil },
		OpenSnapshot: func(context.Context, string, []string) (Snapshot, error) { return passthroughSnapshot{}, nil },
		QueryChunks:  dedup.answer,
		Logger:       quiet(),
	})
	second, err := svc2.run(context.Background(), testJobID, raw)
	if err != nil || second.State != stateSuccess {
		t.Fatalf("second run: %v %+v", err, second)
	}
	if got := uploadedChunkIDs(store2); len(got) != 0 {
		t.Fatalf("a second run over unchanged data uploaded %v", got)
	}
}

// ---------------------------------------------------------------------------
// The batch bound
// ---------------------------------------------------------------------------

func TestNoQueryEverExceedsTheBatchBound(t *testing.T) {
	// FALSIFYING OBSERVATION: a query carrying more than maxChunkBatch ids, or
	// (the fixture-blind failure) a fixture too small to produce more than one
	// batch, which would pass against an implementation that never flushed.
	dir := t.TempDir()
	// At the test dedup parameters (avg 1024) this is several thousand chunks
	// — comfortably more than one batch, so the bound is exercised and not
	// merely satisfied.
	data := patternBytes(4 << 20)
	src := filepath.Join(dir, "big.bin")
	if err := os.WriteFile(src, data, 0o644); err != nil {
		t.Fatal(err)
	}

	store := newFakeStore(testStorageRef)
	svc, dedup := newTestServiceWithDedup(store, quiet())
	res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil || res.State != stateSuccess {
		t.Fatalf("backup.run: %v %+v", err, res)
	}

	total, full := 0, 0
	for i, q := range dedup.queries {
		if len(q.Check) > maxChunkBatch || len(q.Stored) > maxChunkBatch {
			t.Fatalf("query %d carried %d check / %d stored ids; the bound is %d",
				i, len(q.Check), len(q.Stored), maxChunkBatch)
		}
		total += len(q.Check)
		if len(q.Check) == maxChunkBatch {
			full++
		}
	}
	chunks, _ := res.ChunkCount, 0
	if int(chunks) <= maxChunkBatch {
		t.Fatalf("the fixture produced %d chunks, at most one batch; it cannot show the bound "+
			"is applied", chunks)
	}
	if full == 0 {
		t.Fatalf("no query reached the bound across %d chunks in %d queries; the batch is being "+
			"flushed early and the round-trip count is not what it claims",
			chunks, len(dedup.queries))
	}
	if total != int(chunks) {
		t.Fatalf("the run referenced %d distinct chunks and asked about %d", chunks, total)
	}
	t.Logf("%d chunks resolved in %d queries (%d of them full at %d)",
		chunks, len(dedup.queries), full, maxChunkBatch)
}

func TestASixFigureChunkListIsNotASixFigureAllocation(t *testing.T) {
	// The budget question the exchange raises, and the reason a pending entry
	// is a reference rather than bytes.
	//
	// Measured as CUMULATIVE ALLOCATION, never as retained heap read after the
	// job: an after-the-fact reading cannot see a peak, which is the lesson
	// budget_test.go's falsifier 2 records in full. The gate is that backing up
	// N bytes does not allocate N bytes — the same property, asked of the code
	// path that now also holds a batch of hashes.
	//
	// FALSIFYING OBSERVATION: allocation at or above the file's own size, which
	// is what buffering each batch's plaintext until the answer arrives costs.
	//
	// FALSIFICATION MARGIN, measured once with that exact regression planted:
	// this gate (`r.BytesPerOp > 3*fileSize`, below) failed by only 1.09x over
	// its threshold -- marginal, not decisive, unlike budget_test.go's
	// Falsifier3 RSS gate (7.9x / ~1350x baseline) measured against the same
	// planted bug. A flake here should be investigated, not "fixed" by
	// loosening `3*fileSize`; that this gate is close was already known.
	const fileSize = 8 << 20
	dir := t.TempDir()
	src := filepath.Join(dir, "big.bin")
	if err := os.WriteFile(src, patternBytes(fileSize), 0o644); err != nil {
		t.Fatal(err)
	}
	raw := runParamsJSON(t, []string{src}, nil)

	r := budget.Fixed("backup.run-dedup-batching", 1, func() func() {
		store := newFakeStore(testStorageRef)
		svc, _ := newTestServiceWithDedup(store, quiet())
		ctx := context.Background()
		return func() {
			res, err := svc.run(ctx, testJobID, raw)
			if err != nil {
				panic(err)
			}
			if res.State != stateSuccess {
				panic("the measured run failed: " + deref(res.Error))
			}
		}
	})
	// The fake store KEEPS every uploaded object, so this measurement includes
	// a full copy of the sealed data. That is deliberate: the threshold has to
	// be above what the apparatus costs and below what the wrong
	// implementation costs, and buffering a batch's plaintext would add another
	// whole copy on top.
	if r.BytesPerOp > 3*fileSize {
		t.Fatalf("backing up %d bytes allocated %d in total. A batch that held chunk PLAINTEXT "+
			"until the answer arrived would cost about this; a batch of references costs ~100 "+
			"bytes per chunk", fileSize, r.BytesPerOp)
	}
	t.Logf("a %d MiB run: %d allocs, %d bytes allocated in total (limit %d)",
		fileSize>>20, r.AllocsPerOp, r.BytesPerOp, 3*fileSize)
}

// ---------------------------------------------------------------------------
// What the agent refuses to believe
// ---------------------------------------------------------------------------

func TestAnAnswerToADifferentQuestionFailsTheRun(t *testing.T) {
	// Applying one batch's verdict to another batch's chunks silently drops
	// uploads and leaves a manifest referencing objects nobody wrote.
	//
	// FALSIFYING OBSERVATION: a successful run.
	dir := t.TempDir()
	src := filepath.Join(dir, "f.bin")
	if err := os.WriteFile(src, patternBytes(20_000), 0o644); err != nil {
		t.Fatal(err)
	}
	store := newFakeStore(testStorageRef)
	svc, dedup := newTestServiceWithDedup(store, quiet())
	dedup.wrongQueryID = true

	res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil {
		t.Fatalf("an unusable answer is an operational failure, not a refusal: %v", err)
	}
	if res.State != stateFailed {
		t.Fatalf("the run reported %q against an answer to a different query", res.State)
	}
	if got := uploadedChunkIDs(store); len(got) != 0 {
		t.Fatalf("chunks were uploaded on the strength of a mismatched answer: %v", got)
	}
}

func TestAnAnswerNamingAChunkTheBatchDidNotAskAboutFailsTheRun(t *testing.T) {
	// A control plane naming an id this batch never mentioned is asking the
	// agent to write an object whose bytes it does not have. There is no safe
	// reading of it.
	//
	// FALSIFYING OBSERVATION: a successful run, or an object appearing under
	// the injected id.
	dir := t.TempDir()
	src := filepath.Join(dir, "f.bin")
	if err := os.WriteFile(src, patternBytes(20_000), 0o644); err != nil {
		t.Fatal(err)
	}
	injected := strings.Repeat("ab", 32)
	store := newFakeStore(testStorageRef)
	svc, dedup := newTestServiceWithDedup(store, quiet())
	dedup.answerExtra = injected

	res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil {
		t.Fatalf("expected a failed result, got a refusal: %v", err)
	}
	if res.State != stateFailed {
		t.Fatalf("the run reported %q against an answer naming an unasked chunk", res.State)
	}
	for _, id := range uploadedChunkIDs(store) {
		if id == injected {
			t.Fatal("the injected chunk id became an object key")
		}
	}
}

func TestAChunkThatChangesBetweenHashingAndUploadFailsTheRunRatherThanCorruptingTheRepository(t *testing.T) {
	// The re-read guard. A chunk id is BLAKE3 of its plaintext and the
	// repository is content addressed, so storing different bytes under an id
	// another device already references corrupts every backup point that shares
	// it. Under a real snapshot this cannot happen; under passthroughSnapshot
	// it is a live file being written while it is read.
	//
	// FALSIFYING OBSERVATION: a successful run, or an object stored whose
	// decrypted bytes do not hash to its own key.
	dir := t.TempDir()
	src := filepath.Join(dir, "f.bin")
	if err := os.WriteFile(src, patternBytes(20_000), 0o644); err != nil {
		t.Fatal(err)
	}

	store := newFakeStore(testStorageRef)
	dedup := newFakeDedup(testStorageRef)
	svc := New(Options{
		OpenStore:    func(context.Context, Repository) (ObjectStore, error) { return store, nil },
		OpenSnapshot: func(context.Context, string, []string) (Snapshot, error) { return passthroughSnapshot{}, nil },
		// Rewrite the file the moment the control plane is asked about it —
		// i.e. after the first read and hash, before the re-read for upload.
		QueryChunks: func(ctx context.Context, q wire.ChunkQuery) (wire.ChunkGrant, error) {
			if len(q.Check) > 0 {
				// DIFFERENT BYTES, not merely a different length: patternBytes
				// is deterministic by absolute offset, so a longer file of the
				// same pattern re-reads byte-for-byte identically at every
				// offset and the guard would correctly see no change. That
				// fixture would have passed while proving nothing.
				if err := os.WriteFile(src, bytes.Repeat([]byte{0x5A}, 20_000), 0o644); err != nil {
					t.Error(err)
				}
			}
			return dedup.answer(ctx, q)
		},
		Logger: quiet(),
	})

	res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil {
		t.Fatalf("expected a failed result, got a refusal: %v", err)
	}
	if res.State != stateFailed {
		t.Fatalf("the run reported %q after the bytes moved under it", res.State)
	}
	if got := uploadedChunkIDs(store); len(got) != 0 {
		t.Fatalf("objects were written after the bytes moved: %v. Every one of them is now "+
			"stored under a content id its own bytes do not hash to", got)
	}
}

func TestASealedChunkLargerThanTheGrantPermitsIsNotEvenAttempted(t *testing.T) {
	// `content-length-range` is signed INTO the policy, so S3 would reject
	// this; catching it locally turns a mid-run 400 into a legible failure.
	//
	// FALSIFYING OBSERVATION: a successful run, or an object landing in the
	// store despite exceeding the grant.
	dir := t.TempDir()
	src := filepath.Join(dir, "f.bin")
	if err := os.WriteFile(src, patternBytes(20_000), 0o644); err != nil {
		t.Fatal(err)
	}
	store := newFakeStore(testStorageRef)
	svc, dedup := newTestServiceWithDedup(store, quiet())
	dedup.maxBytes = 8 // smaller than any sealed chunk

	res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil {
		t.Fatalf("expected a failed result, got a refusal: %v", err)
	}
	if res.State != stateFailed {
		t.Fatalf("the run reported %q with a grant that permits 8 bytes", res.State)
	}
	if got := uploadedChunkIDs(store); len(got) != 0 {
		t.Fatalf("objects were written past the grant's size condition: %v", got)
	}
}

func TestAnUnreachableControlPlaneIsAFailedRunAndNotARefusal(t *testing.T) {
	// The failed-versus-refused split (see Run's comment): an operational
	// failure must reach `ingest_backup_result`'s `failed` arm, or the
	// `backup_runs` row sits on `running` forever with the error nowhere an
	// operator looks.
	//
	// FALSIFYING OBSERVATION: a jobs.Refusal, or a success.
	dir := t.TempDir()
	src := filepath.Join(dir, "f.bin")
	if err := os.WriteFile(src, patternBytes(20_000), 0o644); err != nil {
		t.Fatal(err)
	}
	store := newFakeStore(testStorageRef)
	svc, dedup := newTestServiceWithDedup(store, quiet())
	dedup.failAt = 1

	res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	var refusal *jobs.Refusal
	if errors.As(err, &refusal) {
		t.Fatalf("an unreachable control plane was reported as a refusal: %v", err)
	}
	if err != nil {
		t.Fatalf("backup.run: %v", err)
	}
	if res.State != stateFailed {
		t.Fatalf("the run reported %q with the control plane unreachable", res.State)
	}
	if strings.Contains(deref(res.Error), src) {
		t.Errorf("the failure reason names a source path: %q", deref(res.Error))
	}
}

// ---------------------------------------------------------------------------
// The grant is what authorizes a write
// ---------------------------------------------------------------------------

func TestTheManifestIsWrittenUnderAGrantToo(t *testing.T) {
	// The manifest is the last write of a run and the easiest one to leave
	// unauthorized, because its key is server-derivable and it is written
	// outside the chunk loop.
	//
	// FALSIFYING OBSERVATION: the fake store's grant check rejecting it (which
	// fails the run), or fewer grants recorded than objects written.
	src := buildFixture(t)
	store := newFakeStore(testStorageRef)
	svc, dedup := newTestServiceWithDedup(store, quiet())

	res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil || res.State != stateSuccess {
		t.Fatalf("backup.run: %v %+v", err, res)
	}
	store.mu.Lock()
	puts, grants := store.puts, len(store.grantsUsed)
	store.mu.Unlock()
	if puts == 0 || grants != puts {
		t.Fatalf("%d writes carried %d grants", puts, grants)
	}
	// The closing exchange exists so the manifest is written under a grant
	// issued AFTER the walk rather than before it.
	if len(dedup.queries) < 2 {
		t.Fatalf("a run issued %d queries; the closing exchange that refreshes the grant and "+
			"reports the last batch is missing", len(dedup.queries))
	}
	last := dedup.queries[len(dedup.queries)-1]
	if len(last.Check) != 0 {
		t.Errorf("the closing exchange asked about %d chunks; it should ask about none",
			len(last.Check))
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

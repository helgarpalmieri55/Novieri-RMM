package backup

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// The chunker's own properties. The round trip proves the bytes come back; it
// does not prove that they come back through CONTENT-DEFINED boundaries, and a
// fixed-size chunker would pass every correctness test in this package while
// destroying the dedup ratio that is the module's entire economic argument.

func chunkAll(t *testing.T, d dedupSpec, data []byte) [][]byte {
	t.Helper()
	c := newChunker(d)
	c.reset(bytes.NewReader(data))
	var out [][]byte
	for {
		ch, err := c.next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		// next() returns a slice into the chunker's buffer, valid only until
		// the next call, so the test must copy. Stated here because a test that
		// forgot would produce mysterious, order-dependent failures.
		out = append(out, bytes.Clone(ch))
	}
	return out
}

func TestChunkingIsLosslessAndRespectsMinAndMax(t *testing.T) {
	// FALSIFYING OBSERVATION: chunks that do not concatenate back to the input,
	// a chunk above max, or a non-final chunk below min.
	d := testDedup()
	for _, n := range []int{0, 1, 511, 512, 513, 1023, 1024, 4095, 4096, 4097, 100_000, 1 << 20} {
		data := make([]byte, n)
		fill(data, uint64(n))
		chunks := chunkAll(t, d, data)

		var joined []byte
		for i, ch := range chunks {
			if len(ch) > d.Max {
				t.Errorf("n=%d: chunk %d is %d bytes, above max %d", n, i, len(ch), d.Max)
			}
			if i < len(chunks)-1 && len(ch) < d.Min {
				t.Errorf("n=%d: non-final chunk %d is %d bytes, below min %d",
					n, i, len(ch), d.Min)
			}
			joined = append(joined, ch...)
		}
		if !bytes.Equal(joined, data) {
			t.Fatalf("n=%d: the chunks do not reassemble to the input (%d bytes out of %d)",
				n, len(joined), n)
		}
		if n == 0 && len(chunks) != 0 {
			t.Errorf("an empty stream produced %d chunks", len(chunks))
		}
	}
}

func TestBoundariesAreContentDefinedAndResyncAfterAnInsert(t *testing.T) {
	// THE PROPERTY THE WHOLE FORMAT RESTS ON. Prepending one byte to a file
	// shifts every byte after it. A fixed-size chunker re-chunks the entire
	// file and re-uploads it; a content-defined one re-synchronises within a
	// chunk or two, which is why an incremental backup of a 4 GiB mailbox costs
	// kilobytes instead of gigabytes.
	//
	// THE MUTATION: one byte prepended to a 512 KiB stream. Asserted to have
	// changed the data before the comparison runs.
	//
	// FALSIFYING OBSERVATION: fewer than 90% of the original chunks surviving
	// the insert. A fixed-size chunker scores ~0%; a correct content-defined one
	// loses only the chunks that straddle the insertion point.
	d := testDedup()
	original := make([]byte, 512<<10)
	fill(original, 0xBEEF)

	mutated := append([]byte{0x42}, original...)
	if bytes.Equal(mutated, original) || len(mutated) != len(original)+1 {
		t.Fatal("the mutation did not apply; this test would pass while measuring nothing")
	}

	before := map[string]int{}
	h := newBlake3()
	for _, ch := range chunkAll(t, d, original) {
		before[chunkID(h, ch)]++
	}
	if len(before) < 100 {
		t.Fatalf("the fixture produced only %d distinct chunks; too few to measure resync",
			len(before))
	}

	survived := 0
	total := 0
	for _, ch := range chunkAll(t, d, mutated) {
		total++
		if before[chunkID(h, ch)] > 0 {
			survived++
		}
	}
	ratio := float64(survived) / float64(len(before))
	if ratio < 0.90 {
		t.Fatalf("only %d of %d original chunks (%.1f%%) survived a ONE-BYTE PREPEND. Content-"+
			"defined chunking exists to make that number close to 100%%; a fixed-size chunker "+
			"scores near zero and this format's dedup ratio would be a fiction",
			survived, len(before), ratio*100)
	}
	t.Logf("one-byte prepend to %d KiB: %d of %d original chunks survived (%.1f%%), %d chunks "+
		"after", len(original)>>10, survived, len(before), ratio*100, total)
}

func TestTheSameBytesAlwaysChunkTheSameWayAcrossReadSizes(t *testing.T) {
	// The buffer-refill path is where a streaming chunker goes wrong, and it
	// goes wrong invisibly: the reassembled data is still correct, but the
	// BOUNDARIES move, so the same file backed up from a fast disk and a slow
	// network share dedups against itself not at all.
	//
	// FALSIFYING OBSERVATION: a different chunk-id sequence for the same bytes
	// delivered in different Read sizes.
	d := testDedup()
	data := make([]byte, 300_000)
	fill(data, 0x5EED)

	want := idsOf(t, chunkAll(t, d, data))
	for _, readSize := range []int{1, 7, 63, 64, 511, 512, 513, 4096, 65536} {
		c := newChunker(d)
		c.reset(&drip{data: data, max: readSize})
		var got []string
		h := newBlake3()
		for {
			ch, err := c.next()
			if errors.Is(err, io.EOF) {
				break
			}
			if err != nil {
				t.Fatal(err)
			}
			got = append(got, chunkID(h, ch))
		}
		if len(got) != len(want) {
			t.Fatalf("read size %d produced %d chunks, want %d", readSize, len(got), len(want))
		}
		for i := range want {
			if got[i] != want[i] {
				t.Fatalf("read size %d: chunk %d is %s, want %s", readSize, i, got[i], want[i])
			}
		}
	}
	t.Logf("%d chunks, identical across read sizes from 1 byte to 64 KiB", len(want))
}

func idsOf(t *testing.T, chunks [][]byte) []string {
	t.Helper()
	h := newBlake3()
	out := make([]string, 0, len(chunks))
	for _, ch := range chunks {
		out = append(out, chunkID(h, ch))
	}
	return out
}

// drip delivers at most `max` bytes per Read, so the chunker's refill path is
// exercised at every alignment.
type drip struct {
	data []byte
	off  int
	max  int
}

func (d *drip) Read(p []byte) (int, error) {
	if d.off >= len(d.data) {
		return 0, io.EOF
	}
	n := len(p)
	if n > d.max {
		n = d.max
	}
	if rem := len(d.data) - d.off; n > rem {
		n = rem
	}
	copy(p, d.data[d.off:d.off+n])
	d.off += n
	return n, nil
}

// ---------------------------------------------------------------------------
// dedup_params validation
// ---------------------------------------------------------------------------

func TestDedupParamsThisAgentWillNotChunkWithAreRefused(t *testing.T) {
	// A chunker parameter this agent silently substituted would fork the
	// repository's chunk domain (spec §5) with no error anywhere — every chunk
	// would miss dedup against every chunk already stored.
	//
	// FALSIFYING OBSERVATION: any of these being accepted, or the valid case
	// being refused.
	base := testDedup()
	for _, c := range []struct {
		name string
		spec dedupSpec
		why  string
	}{
		{"alien-algo", dedupSpec{Algo: "rabin", Hash: base.Hash, Avg: base.Avg, Min: base.Min, Max: base.Max},
			"a chunker this build does not implement"},
		{"alien-hash", dedupSpec{Algo: base.Algo, Hash: "sha256", Avg: base.Avg, Min: base.Min, Max: base.Max},
			"the chunk id IS the dedup key"},
		{"avg-not-power-of-two", dedupSpec{Algo: base.Algo, Hash: base.Hash, Avg: 1000, Min: 512, Max: 4096},
			"the boundary rule is a bit mask"},
		{"min-above-avg", dedupSpec{Algo: base.Algo, Hash: base.Hash, Avg: 1024, Min: 2048, Max: 4096},
			"min <= avg <= max"},
		{"max-absurd", dedupSpec{Algo: base.Algo, Hash: base.Hash, Avg: 1 << 20, Min: 1 << 19, Max: 1 << 31},
			"`max` is the size of the buffer the job holds resident"},
		{"min-tiny", dedupSpec{Algo: base.Algo, Hash: base.Hash, Avg: 1024, Min: 8, Max: 4096},
			"below 512 bytes the manifest outweighs the data"},
	} {
		t.Run(c.name, func(t *testing.T) {
			_, err := c.spec.defaults()
			var refusal *jobs.Refusal
			if !errors.As(err, &refusal) {
				t.Fatalf("%s (%s) was accepted", c.name, c.why)
			}
		})
	}

	// The unmutated case is accepted, so the cases above prove something about
	// the specific value each changed.
	if _, err := base.defaults(); err != nil {
		t.Fatalf("the valid parameters were refused: %v", err)
	}
	// And an EMPTY dedup_params fills in the server's defaults rather than
	// refusing: the schema declares `{}` as the default.
	filled, err := dedupSpec{}.defaults()
	if err != nil {
		t.Fatalf("dedup_params:{} was refused: %v", err)
	}
	if filled.Avg != defaultChunkAvg || filled.Min != defaultChunkMin || filled.Max != defaultChunkMax {
		t.Fatalf("dedup_params:{} filled in %+v", filled)
	}
}

// ---------------------------------------------------------------------------
// Failure reporting
// ---------------------------------------------------------------------------

func TestAnObjectStoreFailureIsAFailedRESULTAndNotAFailedJob(t *testing.T) {
	// app/jobs/engine.py::complete runs a job type's result handler ONLY for a
	// SUCCEEDED job, and `ingest_backup_result` is what moves the `backup_runs`
	// row off `running`. An agent that returned a Go error for "the object
	// store timed out" would produce a failed JOB and a run row stuck on
	// `running` forever, with no error text anywhere an operator looks.
	//
	// This is also the `refused`-versus-`failed` line the module brief draws:
	// "no volume shadow copy service" is refused; "object store timed out" is
	// failed.
	//
	// FALSIFYING OBSERVATION: a Go error out of run(), or a success document.
	src := buildFixture(t)
	store := newFakeStore(testStorageRef)
	store.failPut = func(suffix string) error {
		return fmt.Errorf("object store timed out")
	}
	svc := newTestService(store, quiet())

	res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil {
		t.Fatalf("an object-store failure came back as a Go error, so the job would be `failed` "+
			"and `ingest_backup_result` would never run: %v", err)
	}
	if res.State != stateFailed {
		t.Fatalf("state = %s, want failed", res.State)
	}
	if res.Error == nil || *res.Error == "" {
		t.Fatal("the failed result carries no error text; `backup_runs.error` would be empty")
	}
	if res.ManifestRef != nil {
		t.Fatal("a failed run reported a manifest_ref; a backup point would be created for a " +
			"run that stored nothing")
	}
	// The store must still have been released.
	if !store.closed {
		t.Error("the transport was not closed on the failure path")
	}
	t.Logf("object-store failure reported as state=failed error=%q", *res.Error)
}

func TestASourcePolicyWithNoPathsIsRefusedRatherThanReportingSuccessOverZeroBytes(t *testing.T) {
	// The single worst failure a backup product can have: a policy that
	// protects nothing and reports success, invisible from the console until
	// the day of a restore.
	//
	// FALSIFYING OBSERVATION: a success document with bytes_protected 0.
	for _, sources := range []map[string]any{
		{},
		{"paths": []string{}},
		{"paths": []string{"", "   "}},
		{"include": []string{"/data"}}, // the key the server does NOT use
	} {
		doc := map[string]any{
			"repository_id": "r", "storage_ref": "s", "repo_key": testKey,
			"snapshot": "vss", "sources": sources,
		}
		raw, err := json.Marshal(doc)
		if err != nil {
			t.Fatal(err)
		}
		_, err = parseRunParams(raw)
		var refusal *jobs.Refusal
		if !errors.As(err, &refusal) {
			t.Errorf("sources=%v was accepted", sources)
		}
	}
}

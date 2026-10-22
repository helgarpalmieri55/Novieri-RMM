//go:build linux

package health

// This file carries the ONE budget falsifier that reaches into the linux
// sampler's internals. It lives behind a build tag because `readInto` is
// declared in sampler_linux.go: with the test in the untagged budget_test.go,
// `GOOS=windows go vet ./...` and `GOOS=darwin go vet ./...` failed to compile
// the package at all --
//
//	budget_test.go:349:18: h.readInto undefined (type *hostSampler has no
//	field or method readInto)
//
// and that vet is a CI step (.github/workflows/ci.yml, "windows service code
// compiles"), so the agent job could not go green. The test guarded itself with
// a runtime `if runtime.GOOS != "linux" { t.Skip(...) }`, which cannot help: a
// skip runs after compilation, and compilation is what was failing.

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

func TestFalsifier2_TheFastTickReadPathDoesNotAllocatePerByteOfTheFileItReads(t *testing.T) {
	// THE GATE THE REAL SAMPLER WAS MISSING, and the one that catches the
	// regression sampler_linux.go's own doc comment forbids in as many words:
	// "os.ReadFile would allocate a fresh slice per tick, per check, forever."
	//
	// Nothing was checking that. It was measured, not assumed: with
	// `os.ReadFile` planted in readInto, THE ENTIRE health package stayed green.
	// Falsifier 2c above — the only assertion aimed at the real sampler — read
	// -4,616 bytes and passed, because budget.Retained is read AFTER the tick
	// returns and a per-tick slice that is freed each tick is garbage by then.
	// That is the same blind spot an after-the-fact RSS reading has; backup's
	// falsifier 2 records it with a number.
	//
	// The end-to-end per-tick figure separates the two by only ~2.3x (6,370
	// bytes clean against 14,466 planted), which is too narrow to gate on
	// comfortably — and it is machine-dependent, because it includes the size
	// of this host's /proc/meminfo. So the assertion is made HERE instead, on
	// readInto directly, where it is both sharp and machine-independent:
	//
	// FEED IT A FILE FAR LARGER THAN ITS BUFFER. readInto's contract is that it
	// fills ONE reused 8 KiB scratch buffer with a single Read, so its cost is
	// the same for a 4 MiB file as for a 400-byte one — /proc files are small
	// and only the head is ever wanted. os.ReadFile's cost is the whole file.
	// The gap is the file's size, and it can be made as large as we like.
	//
	// FALSIFYING OBSERVATION: allocation scaling with the size of the file read.
	//
	// The `if runtime.GOOS != "linux" { t.Skip(...) }` that used to stand here
	// is gone: this file's build tag is what excludes the test now, and the tag
	// is what the runtime skip could never be. See the file header.

	const bigSize = 4 << 20
	big := filepath.Join(t.TempDir(), "big")
	if err := os.WriteFile(big, bytes.Repeat([]byte("cpu 1 2 3 4 5 6 7 8\n"), bigSize/20), 0o600); err != nil {
		t.Fatal(err)
	}
	small := filepath.Join(t.TempDir(), "small")
	if err := os.WriteFile(small, []byte("cpu 1 2 3 4 5 6 7 8\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	read := func(path string) func() func() {
		return func() func() {
			h := &hostSampler{}
			// Warm the lazily allocated scratch buffer outside the measurement,
			// so its one-off 8 KiB is not charged to the first iteration.
			if _, ok := h.readInto(path); !ok {
				panic("readInto failed on " + path)
			}
			return func() {
				raw, ok := h.readInto(path)
				if !ok {
					panic("readInto failed")
				}
				// Not vacuous: the read really did produce bytes.
				if len(raw) == 0 {
					panic("readInto returned nothing")
				}
			}
		}
	}

	rSmall := budget.Measure("readInto-small", read(small))
	rBig := budget.Measure("readInto-4MiB", read(big))
	if rSmall.N == 0 || rBig.N == 0 {
		t.Fatal("the benchmark ran zero iterations; the measurement is vacuous")
	}

	// The whole point is that reading a 4 MiB file costs what reading a 20-byte
	// one costs. The allowance is 8 KiB — one scratch buffer's worth of slack,
	// and 1/512th of the file. A reused-buffer implementation reads at most
	// 8192 bytes whatever the file's size; os.ReadFile allocates 4 MiB and then
	// some, because it grows its slice as it goes.
	const allowance = 8 << 10
	if delta := rBig.BytesPerOp - rSmall.BytesPerOp; delta > allowance {
		t.Fatalf("reading a %d-byte file allocated %d bytes more per call than reading a 20-byte "+
			"one (allowance %d). readInto must fill ONE reused buffer with a single Read, so its "+
			"cost is independent of the file's size; this one allocates per byte of the file, "+
			"which on the fast tick is a fresh slice per check per tick forever",
			bigSize, delta, allowance)
	}

	// AND AN ABSOLUTE BOUND, because a ratio alone would be satisfied by an
	// implementation that allocated 4 MiB for BOTH files.
	if rBig.BytesPerOp > allowance {
		t.Fatalf("one readInto of a %d-byte file allocates %d bytes (%d allocs); the reused "+
			"buffer is 8192 bytes and the ceiling is %d",
			bigSize, rBig.BytesPerOp, rBig.AllocsPerOp, allowance)
	}
	t.Logf("falsifier 2d: readInto of a 20-byte file = %d B/op (%d allocs), of a %d-byte file = "+
		"%d B/op (%d allocs); difference %d (allowance %d)",
		rSmall.BytesPerOp, rSmall.AllocsPerOp, bigSize, rBig.BytesPerOp, rBig.AllocsPerOp,
		rBig.BytesPerOp-rSmall.BytesPerOp, allowance)
}

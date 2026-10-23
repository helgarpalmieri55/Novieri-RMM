package deploy

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

// THE `script.run` / `app.deploy` IDLE BUDGET, MEASURED.
//
// CLAUDE.md: "Heavy capabilities (patch, backup, remote, imaging) stay dormant
// and only allocate/initialize when a job for them actually runs, then
// release." These two are as heavy as capabilities get — a process spawn and a
// multi-gigabyte download — so the dormancy claim is the one that matters, and
// it is measured here rather than described.
//
// # Why none of this is a Benchmark function
//
// `go test -bench` EXITS 0 ON A REGRESSION. Measured on this project, not
// assumed: internal/budget's package comment records a planted 64 KiB/op
// regression printing `65536 B/op` and still exiting 0. A budget stated as a
// benchmark is a log line, not a gate.
//
// So every number below is produced by `testing.Benchmark` called from an
// ORDINARY Test (through internal/budget's harness) and COMPARED against a
// threshold. A regression turns CI red.
//
// # The falsifiers this capability owes
//
//  1. Anything resident with no deploy or script job in flight: an allocation,
//     a goroutine, an open file descriptor, an HTTP connection pool.
//  2. Resident memory that does not return to its pre-job baseline after one
//     job, or that scales with the number of jobs.
//  3. A per-job cost that grows with the SIZE of the script body or the
//     installer's output — the two inputs an operator controls.
//
// # What is NOT asserted here: wall-clock CPU
//
// Same refusal, for the same reason, as internal/budget's package comment and
// site's budget file: a shared runner's variance exceeds the signal, a
// threshold loose enough not to flake is loose enough to pass a real
// regression, and a flaky budget test gets skipped — which reads as coverage
// while measuring nothing. `~0% CPU at idle` stays a DESIGN TARGET for this
// capability, approached through the machine-independent proxies below.

// ---------------------------------------------------------------------------
// Falsifier 1: anything resident with no job in flight
// ---------------------------------------------------------------------------

func TestFalsifier1_AnIdleAgentHoldsNothingForEitherCapability(t *testing.T) {
	// The only code path that runs while the agent is idle and these
	// capabilities are compiled in is the registry lookup the runner performs
	// per inbound frame, plus constructing the Service. Neither may allocate,
	// because both happen on an agent that is doing nothing.
	//
	// This is the assertion that catches the obvious wrong design: an
	// http.Client, a connection pool, a scratch directory or a worker
	// goroutine created at construction rather than inside a job.
	//
	// FALSIFYING OBSERVATION: AllocsPerOp > 0, or a goroutine delta.
	r := budget.AssertNoAllocations(t, "deploy dormant", func() func() {
		s := New(Options{Logger: quiet()})
		return func() {
			if s.Scripts() != 0 || s.Deploys() != 0 {
				panic("the dormancy measurement ran a job")
			}
		}
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("falsifier 1: %d iterations, %d allocs/op, %d B/op, goroutine delta %d",
		r.N, r.AllocsPerOp, r.BytesPerOp, r.Goroutines)
}

func TestFalsifier1_NoFileDescriptorIsOpenBeforeAJobArrivesOrAfterOneEnds(t *testing.T) {
	// The half an allocation count cannot see. A capability that opened its
	// scratch directory once and kept it, or that left the artifact's response
	// body unclosed, allocates nothing per idle tick and still holds an fd per
	// job forever.
	//
	// FALSIFYING OBSERVATION: the open-fd count higher after a hundred
	// complete jobs than it was before the first one. Reads /proc/self/fd, so
	// Linux only — which is the CI platform.
	if _, err := os.ReadDir("/proc/self/fd"); err != nil {
		t.Skip("reads /proc/self/fd; CI is linux")
	}
	root := t.TempDir()
	noop := func(context.Context, Command) (Outcome, error) { return Outcome{}, nil }
	// Two Services because the two capabilities target different platforms:
	// `bash` is a Linux/macOS interpreter and `msi` is a Windows installer
	// type, and a single GOOS cannot exercise both. Both share one scratch
	// root, which is what the emptiness check at the end is about.
	scripts := New(Options{
		GOOS: "linux", LookPath: fakePaths("bash"), Logger: quiet(), ScratchRoot: root,
		Exec: noop,
	})
	deploys := New(Options{
		GOOS: "windows", LookPath: fakePaths("msiexec.exe"), Logger: quiet(), ScratchRoot: root,
		Exec: noop, Fetch: budgetArtifact.fetcher(),
	})

	// Construction alone must open nothing.
	before := openFDs(t)

	script := scriptParamsJSON(t, "echo hello", nil)
	deployJob := budgetDeployParams(t)
	for i := 0; i < 100; i++ {
		if _, err := scripts.runScript(context.Background(), script); err != nil {
			t.Fatal(err)
		}
		if _, err := deploys.deploy(context.Background(), deployJob); err != nil {
			t.Fatal(err)
		}
	}
	after := openFDs(t)
	if after > before {
		t.Fatalf("open file descriptors went from %d to %d across 100 script runs and 100 "+
			"deployments; something is held between jobs", before, after)
	}
	// And the scratch root is empty: no job's directory outlived it.
	entries, err := os.ReadDir(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("%d scratch directories survived their jobs", len(entries))
	}
	t.Logf("falsifier 1 (fds): %d open before, %d after 100 script runs + 100 deployments",
		before, after)
}

func openFDs(t *testing.T) int {
	t.Helper()
	entries, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Fatal(err)
	}
	return len(entries)
}

// ---------------------------------------------------------------------------
// Falsifier 2: memory that does not return to its pre-job baseline
// ---------------------------------------------------------------------------

func TestFalsifier2_ResidentMemoryReturnsToItsPreJobBaseline(t *testing.T) {
	// Measured with budget.Fixed and NOT with testing.Benchmark, for the
	// reason recorded in Fixed's own doc comment: testing.Benchmark picks its
	// own iteration count, and an RSS threshold against an unstated number of
	// iterations measures a GARBAGE RATE rather than a footprint.
	//
	// FALSIFYING OBSERVATION: RSSDelta above the limit, printed in bytes, for a
	// STATED job count.

	// (a) ONE JOB, and RSS back where it started.
	const oneJobLimit = 256 << 10
	one := budget.Fixed("script.run-x1", 1, oneScript(t))
	if one.RSSDelta > oneJobLimit && !raceDetector {
		t.Fatalf("RSS grew %d bytes across ONE script run (limit %d, rss_is_heap_proxy=%v)",
			one.RSSDelta, oneJobLimit, one.RSSIsProxy)
	}
	t.Logf("falsifier 2a: 1 script run, RSS delta %d bytes (proxy=%v), %d allocs, %d bytes "+
		"allocated", one.RSSDelta, one.RSSIsProxy, one.AllocsPerOp, one.BytesPerOp)

	// (b) THE SAME QUESTION OVER A WORKING LIFE. One job returning to baseline
	//     is necessary and not sufficient: a capability that kept 64 bytes per
	//     job would pass (a) forever and still be a footprint bug on a device
	//     that has been up for a month.
	//
	//     2,000 jobs rather than site.wake's 10,000, and the number is stated
	//     because it has to be: each job here creates a directory, writes a
	//     file and removes both, so this is bounded by the filesystem rather
	//     than by arithmetic. 2,000 script runs is about a year of a device
	//     receiving five scripts a day.
	//
	//     The limit is 4 MiB and it is deliberately well clear of the
	//     measurement rather than snug against it: a threshold set at the top
	//     of the observed spread would flake, and a flaky budget test gets
	//     skipped. The retention check in (c) is the sharp instrument; this is
	//     the coarse guard against a genuine leak, which would be tens of MB.
	const lifetimeLimit = 4 << 20
	const jobs = 2_000
	r := budget.Fixed("script.run-x2000", jobs, oneScript(t))
	if r.RSSDelta > lifetimeLimit && !raceDetector {
		t.Fatalf("RSS grew %d bytes across %d script runs (limit %d, rss_is_heap_proxy=%v)",
			r.RSSDelta, r.N, lifetimeLimit, r.RSSIsProxy)
	}
	t.Logf("falsifier 2b: %d script runs, RSS delta %d bytes (proxy=%v), %d allocs/op, %d B/op, "+
		"goroutine delta %d", r.N, r.RSSDelta, r.RSSIsProxy, r.AllocsPerOp, r.BytesPerOp,
		r.Goroutines)

	// (c) THE HALF AN RSS READING CANNOT ANSWER. Allocation that is FREED never
	//     shows as growth; allocation that is RETAINED — a slice of every
	//     result, a map keyed by script id — is invisible to a per-op count and
	//     only appears after a device has been up for weeks. Compared across a
	//     HUNDREDFOLD spread, because internal/budget already measured that a
	//     tenfold spread does not catch a planted 24-byte-per-tick leak.
	const few, many = 20, 2_000
	small := budget.Retained(few, oneScript(t))
	large := budget.Retained(many, oneScript(t))
	const allowance = 64 << 10
	if delta := large - small; delta > allowance {
		t.Fatalf("%d script runs retained %d bytes more than %d (allowance %d); something "+
			"accumulates per job", many, delta, few, allowance)
	}
	t.Logf("falsifier 2c: retained heap %d runs = %d bytes, %d runs = %d bytes, delta = %d",
		few, small, many, large, large-small)
}

func TestFalsifier2_ADeploymentAlsoReturnsToItsBaseline(t *testing.T) {
	// The same question for the heavier of the two. A deployment downloads,
	// hashes, stages and spawns; every one of those is a place to keep
	// something.
	const jobs = 1_000
	const lifetimeLimit = 4 << 20
	r := budget.Fixed("app.deploy-x1000", jobs, oneDeploy(t))
	if r.RSSDelta > lifetimeLimit && !raceDetector {
		t.Fatalf("RSS grew %d bytes across %d deployments (limit %d, rss_is_heap_proxy=%v)",
			r.RSSDelta, r.N, lifetimeLimit, r.RSSIsProxy)
	}
	t.Logf("app.deploy: %d deployments, RSS delta %d bytes (proxy=%v), %d allocs/op, %d B/op, "+
		"goroutine delta %d", r.N, r.RSSDelta, r.RSSIsProxy, r.AllocsPerOp, r.BytesPerOp,
		r.Goroutines)

	const few, many = 10, 1_000
	small := budget.Retained(few, oneDeploy(t))
	large := budget.Retained(many, oneDeploy(t))
	const allowance = 64 << 10
	if delta := large - small; delta > allowance {
		t.Fatalf("%d deployments retained %d bytes more than %d (allowance %d)",
			many, delta, few, allowance)
	}
	t.Logf("app.deploy retained heap: %d = %d bytes, %d = %d bytes, delta = %d",
		few, small, many, large, large-small)
}

// ---------------------------------------------------------------------------
// Falsifier 3: a per-job cost that scales with an operator-controlled input
// ---------------------------------------------------------------------------

func TestTheCostOfOneJobIsBoundedAndTheBoundIsChecked(t *testing.T) {
	// Neither falsifier above covers the per-job cost, and it is the number a
	// regression would move first: a version that read the script back to
	// verify it, or unmarshalled params into a map[string]any, would allocate
	// an order of magnitude more while every assertion above stayed green (the
	// cost is transient, so it neither stays resident nor accumulates).
	//
	// The measurement includes the scratch directory and the file write —
	// those are what a script run actually costs — and excludes the process,
	// which is the OS's cost and not this package's.
	//
	// FALSIFYING OBSERVATION: bytes-per-job above the ceiling.
	const ceiling = 64 << 10
	r := budget.Measure("script.run-one-job", oneScript(t))
	if r.N == 0 {
		t.Fatal("the benchmark ran zero iterations; the measurement is vacuous")
	}
	if r.BytesPerOp > ceiling {
		t.Fatalf("one script run allocates %d bytes/op (%d allocs) over %d iterations, above "+
			"the %d ceiling. CLAUDE.md: 'Any change that regresses idle footprint is a defect.'",
			r.BytesPerOp, r.AllocsPerOp, r.N, ceiling)
	}
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("one script run: %d iterations, %d allocs/op, %d B/op (ceiling %d), goroutine delta %d",
		r.N, r.AllocsPerOp, r.BytesPerOp, ceiling, r.Goroutines)
}

func TestAFloodOfInstallerOutputCostsABoundedAmountRegardlessOfItsSize(t *testing.T) {
	// The one input an operator does NOT control and an installer does. A
	// hundredfold more output must not cost a hundredfold more memory, or a
	// chatty installer is a memory incident.
	//
	// FALSIFYING OBSERVATION: allocation scaling with the length of the
	// stream. Compared across a hundredfold spread, for the reason
	// internal/budget's Retained documents.
	small := budget.Measure("wireLog-1KiB", floodedLog(1<<10))
	large := budget.Measure("wireLog-100MiB", floodedLog(100<<20))
	if large.BytesPerOp > 4*maxLogChars+(4<<10) {
		t.Fatalf("rendering a 100 MiB stream allocates %d bytes/op; the wire cap is %d, so the "+
			"cost must be a small multiple of that and not of the input", large.BytesPerOp,
			maxLogChars)
	}
	t.Logf("wireLog: 1 KiB input = %d B/op, 100 MiB input = %d B/op (wire cap %d)",
		small.BytesPerOp, large.BytesPerOp, maxLogChars)

	// And the capture buffer itself: 100 MiB in, at most maxCapture retained.
	b := &capBuffer{limit: maxCapture}
	chunk := bytes.Repeat([]byte("A"), 1<<16)
	for i := 0; i < 1600; i++ {
		if _, err := b.Write(chunk); err != nil {
			t.Fatal(err)
		}
	}
	if got := len(b.String()); got > maxCapture {
		t.Fatalf("capBuffer held %d bytes of a 100 MiB stream; the cap is %d", got, maxCapture)
	}
	t.Logf("capBuffer: 100 MiB written, %d bytes retained (cap %d)", len(b.String()), maxCapture)
}

func TestALargeArtifactIsStreamedToDiskAndNotHeldInMemory(t *testing.T) {
	// THE ASSERTION THIS CAPABILITY MOST NEEDED AND DID NOT HAVE.
	//
	// `stage` downloads an installer, hashes it and writes it to the scratch
	// directory. maxArtifactBytes is 2 GiB — a Visual Studio bootstrapper, a SQL
	// Server ISO, a CAD suite. The obvious wrong implementation is
	// `io.ReadAll(body)` followed by a write: it is shorter, it passes every
	// correctness test in this package, and it makes a process whose budget is
	// "low-tens-of-MB" hold up to two gigabytes.
	//
	// Every other assertion in this file was blind to it, and that was measured
	// rather than assumed. With `io.ReadAll` planted in stage(), the ENTIRE
	// deploy package stayed green. Two separate reasons, and both had to be
	// fixed:
	//
	//  1. NO TEST EVER DOWNLOADED A LARGE ARTIFACT. budgetArtifact is the
	//     43-byte string "MZ a small but perfectly adequate installer", so
	//     slurping it costs 43 bytes and there is nothing for any gate to see.
	//     A footprint assertion whose fixture is smaller than the footprint is
	//     decoration however it is measured.
	//  2. THE GATES ARE READ AFTER THE JOB RETURNS. budget.Fixed's RSSDelta and
	//     budget.Retained both measure what is resident once the handler is
	//     done, and the buffer is freed before then. backup's falsifier 2
	//     records the same finding with a number: a planted io.ReadAll there
	//     read -131,072 bytes of RSS delta and PASSED. An after-the-fact
	//     reading cannot see a peak.
	//
	// So this test supplies a large artifact and gates on CUMULATIVE
	// ALLOCATION, which is deterministic, machine-independent, and the one
	// number that can see a transient buffer.
	//
	// THE THRESHOLD IS THE ARTIFACT'S OWN SIZE, because that is exactly the
	// property under test: staging an installer must not allocate as much
	// memory as the installer. A streaming implementation allocates io.Copy's
	// 32 KiB buffer and the sha256 state, and nothing per byte.
	//
	// FALSIFYING OBSERVATION: staging a 64 MiB artifact allocating 64 MiB.
	// Measured 53,616 bytes streaming against 393,496,016 with io.ReadAll
	// planted — a factor of 7,300.
	const artifactSize = 64 << 20

	r := budget.Fixed("app.deploy-64MiB-artifact", 1, oneLargeDeploy(t, artifactSize))

	if r.BytesPerOp > artifactSize {
		t.Fatalf("staging a %d-byte artifact allocated %d bytes in total. A streaming "+
			"implementation allocates a copy buffer and the hash state and nothing per byte; "+
			"this one allocated as much as the artifact, which means the download was read into "+
			"memory instead of streamed to disk", artifactSize, r.BytesPerOp)
	}

	// RSS is LOGGED AND NOT GATED here, and the reason is stated rather than
	// left as an omission: it is read after the job returns, which is precisely
	// the blind spot this test exists to close. Gating on it would re-create the
	// hole. The allocation assertion above is the deterministic version of the
	// same question.
	t.Logf("large artifact: one %d MiB deployment allocated %d bytes in total (%d allocs, "+
		"limit %d = the artifact size); RSS delta %d bytes (proxy=%v, not gated)",
		artifactSize>>20, r.BytesPerOp, r.AllocsPerOp, artifactSize, r.RSSDelta, r.RSSIsProxy)
}

// ---------------------------------------------------------------------------
// Apparatus
// ---------------------------------------------------------------------------

// patternReader emits n bytes of a position-derived pattern WITHOUT EVER
// HOLDING THEM.
//
// That is the whole point of it: a 64 MiB fixture built as a []byte would be
// 64 MiB of the TEST'S OWN footprint sitting inside the measurement, which is
// the mistake backup's oneRun comment records health being bitten by. This
// generates its bytes as they are read and retains a constant amount.
//
// The pattern is derived from the ABSOLUTE OFFSET rather than from a repeating
// block, so the byte stream is identical regardless of how the consumer chunks
// its reads. That matters more than it looks: the whole point is to compare a
// streaming reader (io.Copy, fixed 32 KiB reads) against a buffering one
// (io.ReadAll, geometrically growing reads), and a fixture whose CONTENT
// depended on read size would hash differently between the two and fail the
// digest check instead of the budget assertion.
type patternReader struct {
	remaining int
	off       int
}

func (p *patternReader) Read(dst []byte) (int, error) {
	if p.remaining <= 0 {
		return 0, io.EOF
	}
	n := len(dst)
	if n > p.remaining {
		n = p.remaining
	}
	for i := 0; i < n; i++ {
		dst[i] = byte((p.off + i) * 31 % 251)
	}
	p.off += n
	p.remaining -= n
	return n, nil
}

func (p *patternReader) Close() error { return nil }

// oneLargeDeploy returns a setup producing one complete deployment of an
// `size`-byte artifact.
//
// The digest is computed ONCE, in setup, by streaming the same generator — so
// the params carry a hash that genuinely matches and the run really does reach
// the rename rather than being refused at the digest comparison. A refused job
// would allocate nothing and the measurement would be vacuous, which is the
// failure mode this comment exists to prevent.
func oneLargeDeploy(t *testing.T, size int) func() func() {
	t.Helper()

	h := sha256.New()
	if _, err := io.Copy(h, &patternReader{remaining: size}); err != nil {
		t.Fatal(err)
	}
	sum := hex.EncodeToString(h.Sum(nil))

	raw := deployParamsJSON(t, map[string]any{
		"artifact_ref":    "https://packages.example/big.msi",
		"artifact_sha256": sum,
	})
	root := t.TempDir()

	return func() func() {
		s := New(Options{
			GOOS: "windows", LookPath: fakePaths("msiexec.exe"), Logger: quiet(),
			ScratchRoot: root,
			Fetch: func(context.Context, string) (io.ReadCloser, error) {
				return &patternReader{remaining: size}, nil
			},
			Exec: func(context.Context, Command) (Outcome, error) {
				return Outcome{ExitCode: 0}, nil
			},
		})
		ctx := context.Background()
		return func() {
			res, err := s.deploy(ctx, json.RawMessage(raw))
			if err != nil {
				panic(err)
			}
			// Not vacuous: a digest mismatch or a refusal would allocate almost
			// nothing and pass the budget assertion while proving nothing.
			if len(res.Outcomes) != 1 || res.Outcomes[0].State != stateInstalled {
				panic("the measured deployment did not install")
			}
		}
	}
}

// The rigs below RETAIN NOTHING. The behavioural spies elsewhere in this
// package keep every Command so they can be asserted on, and that retention is
// invisible until you measure retained heap: 2,000 jobs of Commands is the
// test's own apparatus reported as the capability's footprint. site's budget
// file records health being bitten by exactly this.

// oneScript returns a setup producing one complete script run, with a
// non-executing Executor and a scratch root that is cleaned up by the test.
func oneScript(t *testing.T) func() func() {
	t.Helper()
	raw := scriptParamsJSON(t, "echo hello\nexit 0\n", nil)
	root := t.TempDir()
	return func() func() {
		s := New(Options{
			GOOS: "linux", LookPath: fakePaths("bash"), Logger: quiet(), ScratchRoot: root,
			Exec: func(context.Context, Command) (Outcome, error) {
				return Outcome{ExitCode: 0, Output: "hello\n"}, nil
			},
		})
		ctx := context.Background()
		// One warm-up outside the measurement, so a lazily initialised anything
		// is not charged to the first iteration.
		if _, err := s.runScript(ctx, raw); err != nil {
			panic(err)
		}
		return func() {
			if _, err := s.runScript(ctx, json.RawMessage(raw)); err != nil {
				panic(err)
			}
		}
	}
}

var budgetArtifact = newArtifact("MZ a small but perfectly adequate installer")

func budgetDeployParams(t *testing.T) json.RawMessage {
	t.Helper()
	return deployParamsJSON(t, map[string]any{
		"artifact_ref":    "https://packages.example/pkg.msi",
		"artifact_sha256": budgetArtifact.sha,
	})
}

func oneDeploy(t *testing.T) func() func() {
	t.Helper()
	raw := budgetDeployParams(t)
	root := t.TempDir()
	return func() func() {
		s := New(Options{
			GOOS: "windows", LookPath: fakePaths("msiexec.exe"), Logger: quiet(),
			ScratchRoot: root,
			Fetch:       budgetArtifact.fetcher(),
			Exec: func(context.Context, Command) (Outcome, error) {
				return Outcome{ExitCode: 0}, nil
			},
		})
		ctx := context.Background()
		if _, err := s.deploy(ctx, raw); err != nil {
			panic(err)
		}
		return func() {
			if _, err := s.deploy(ctx, json.RawMessage(raw)); err != nil {
				panic(err)
			}
		}
	}
}

func floodedLog(n int) func() func() {
	return func() func() {
		out := Outcome{Output: strings.Repeat("installer chatter\n", n/18), Truncated: true}
		return func() {
			if len(wireLog(out)) > maxLogChars+64 {
				panic("the wire log exceeded its cap")
			}
		}
	}
}

// Compile-time reminder that the artifact fixture's digest really is its
// digest: budgetArtifact is used by the fd test above, where a hash mismatch
// would make every iteration a refusal and the measurement vacuous.
func init() {
	sum := sha256.Sum256(budgetArtifact.body)
	if hex.EncodeToString(sum[:]) != budgetArtifact.sha {
		panic("the budget fixture's digest is not its digest")
	}
	var _ io.ReadCloser = io.NopCloser(bytes.NewReader(nil))
}

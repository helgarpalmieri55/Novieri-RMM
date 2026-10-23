package health

// The posture sampler.
//
// It is a SEPARATE object from the fast-tick sampler, on every platform, and
// that separation is the mechanism behind §0.5 falsifier 3: posture probes
// running on the fast tick instead of the posture sub-tick is a DEFECT, and the
// observation that catches it is this object's call counter staying at zero
// across a run of fast ticks.
//
// Everything here is bounded on three axes, because these probes spawn
// processes on Linux and macOS:
//
//   - a 5s deadline and cmd.WaitDelay per command;
//   - a 20s budget for a whole PASS, shared by every command in it; and
//   - an overlap guard, so a caller landing on top of a pass in flight gets
//     `unavailable` instead of a second set of shell-outs. What it defends
//     against is stated at Sample, because the reason first written here was
//     not true.
//
// The pass budget is the axis that bounds what an operator would actually
// notice, and the honest statement of what it buys is this: a posture pass CAN
// hold the agent's scheduler wheel, for up to posturePassBudget PLUS one
// postureWaitDelay (22s, not 20s), once per posture interval (>= 900s,
// policy.go). A command started just before passDeadline has its ctx expire
// at passDeadline, and cmd.WaitDelay then still extends cmd.Wait by up to
// postureWaitDelay (execPostureRunner) -- that tail is not multiplied,
// because Go's exec.Cmd.Start returns ctx.Err() immediately for an
// already-done context, so the rest of a ladder after the deadline costs
// nothing. It is bounded, not eliminated. The per-command deadline alone does
// not bound it, because a pass issues several commands and 5s each
// MULTIPLIES: Engine.onTick holds `e.mu` across its whole sample loop and
// internal/scheduler invokes wheel callbacks synchronously, so the wheel is
// held for the sum, and `health.check.now` queues behind `e.mu`. Before the
// budget existed a machine with two wedged disks stalled every scheduled
// entry on the agent for about a minute.
//
// The guard returns `unavailable`; it is NOT a cache. §4.3 forbids holding a
// sample between ticks and this holds none.
//
// A probe answers `true` or `false` only for a positive parsed statement it
// obtained itself; every other outcome -- the tool missing, a timeout, output
// it cannot parse, an error -- reports `unavailable()` rather than guessing.

import (
	"bytes"
	"context"
	"errors"
	"os/exec"
	"sync"
	"time"
)

const (
	postureDeadline  = 5 * time.Second
	postureWaitDelay = 2 * time.Second
	maxPostureOutput = 1 << 20 // 1 MiB

	// posturePassBudget bounds ONE posture pass -- every Sample call the engine
	// makes for a single tick, taken together -- and not one command.
	//
	// 20s is four per-command deadlines. That is deliberate on both sides:
	//
	//   - Not smaller: a healthy Linux pass is at most ~4 commands plus one
	//     smartctl per disk, each answering in milliseconds, so a real pass
	//     spends a thousandth of this. Four full deadlines leaves room for one
	//     or two genuinely wedged commands before the budget itself is spent.
	//
	//     Corrected 2026-08-13: that headroom is ORDER-DEPENDENT, not a
	//     guarantee for "the remaining checks" as this comment used to claim.
	//     `pol.checks` is server-supplied and `Engine.onTick` iterates it by
	//     index, same order, every tick (policy.go, engine.go). A check wedged
	//     EARLY in that order consumes the pass budget by itself, and every
	//     check after it reports `unavailable` having run no command --- not
	//     because it wedged, but because nothing was left of the budget by the
	//     time its turn came. Nothing rotates the order, so this repeats on
	//     every pass, forever, until the wedged check is fixed or dropped from
	//     policy. That is inherent to a whole-pass budget that bounds the
	//     wheel rather than a defect in this constant: the alternative this
	//     budget replaced was an unbounded stall (see the header). See
	//     TestPostureOrderAWedgedCheckStarvesEverythingAfterItInPolicyOrder.
	//   - Not larger: this is time the whole agent's scheduler wheel is held
	//     (see the header). At the 900s minimum posture interval it is 2.2% of
	//     the interval, worst case, and no other wheel entry can be later than
	//     20s because of health.
	posturePassBudget = 20 * time.Second

	// posturePassGap is how this object tells one pass from the next WITHOUT a
	// context on the Sampler interface.
	//
	// Sampler.Sample takes no ctx and must not grow one: main.go, engine.go and
	// §0.5 falsifier 3 are all written against the two-string signature. So the
	// pass boundary is derived from the sampler's own state -- the gap since
	// the previous Sample call RETURNED. Within a pass the engine's work
	// between two checks is a comparison and an integer write (§4.2), i.e.
	// microseconds; between two passes the gap is the posture interval, which
	// policy.go floors at 900 seconds. Six orders of magnitude separate them,
	// so any threshold in between works and 1s is chosen well clear of a
	// scheduling hiccup.
	//
	// Both ways of being wrong are safe, and they are asymmetric on purpose:
	// too LONG a gap would fold a `health.check.now` arriving seconds after a
	// tick into that tick's spent budget and answer it `unavailable`; too SHORT
	// merely restarts the budget mid-pass, so a pathological pass runs longer
	// than 20s. Neither can produce a definite `true`/`false` that nothing
	// measured, which is the property §5 protects.
	posturePassGap = 1 * time.Second
)

var errNotInstalled = errors.New("health: the tool is not installed")

type postureResult struct {
	Stdout   string
	Stderr   string
	ExitCode int
	Err      error
	// NotFound separates "this tool is not installed" from "this tool ran and
	// answered". `firewall-cmd --state` exits non-zero to say "not running",
	// which is a real answer; a missing binary is not.
	NotFound bool
}

func (r postureResult) ok() bool { return r.Err == nil }

// postureRunner is the injected seam the shell-out probes take, so Linux's and
// macOS's decision-making is unit-tested on a machine that has neither tool.
//
// patch's `runner` is the same shape but unexported in its own package. It is
// duplicated here rather than shared, per the convention stated at
// internal/capabilities/netsweep/netsweep_test.go:1240.
type postureRunner func(ctx context.Context, name string, args ...string) postureResult

func execPostureRunner(ctx context.Context, name string, args ...string) postureResult {
	path, err := exec.LookPath(name)
	if err != nil {
		return postureResult{ExitCode: -1, Err: err, NotFound: true}
	}
	ctx, cancel := context.WithTimeout(ctx, postureDeadline)
	defer cancel()

	cmd := exec.CommandContext(ctx, path, args...)
	// WaitDelay is NOT OPTIONAL, and it was measured on this project rather
	// than reasoned about (patch/exec.go and inventory/exec.go independently
	// record the same finding). CommandContext's cancellation kills only the
	// direct child; cmd.Wait then blocks until the stdout pipe closes, and a
	// GRANDCHILD that inherited it keeps it open. A 200ms deadline took 30.003s
	// before this line existed.
	cmd.WaitDelay = postureWaitDelay

	var out, errb postureBoundedBuffer
	out.limit, errb.limit = maxPostureOutput, maxPostureOutput
	cmd.Stdout, cmd.Stderr = &out, &errb

	runErr := cmd.Run()
	res := postureResult{Stdout: out.String(), Stderr: errb.String(), Err: runErr}
	var ee *exec.ExitError
	switch {
	case runErr == nil:
		res.ExitCode = 0
	case errors.As(runErr, &ee):
		res.ExitCode = ee.ExitCode()
	default:
		res.ExitCode = -1
	}
	return res
}

// postureBoundedBuffer refuses writes past limit. A cap enforced by truncating
// AFTERWARDS is not a cap: the memory was already allocated.
type postureBoundedBuffer struct {
	buf   bytes.Buffer
	limit int
}

func (b *postureBoundedBuffer) Write(p []byte) (int, error) {
	n := len(p)
	if room := b.limit - b.buf.Len(); room > 0 {
		if len(p) > room {
			p = p[:room]
		}
		b.buf.Write(p)
	}
	// io.Writer requires n == len(p) whenever err == nil (see the io.Writer
	// doc comment). Reporting the TRUNCATED length here is a short write in
	// disguise: exec.Cmd copies a non-*os.File Stdout/Stderr through an
	// internal io.Copy goroutine, and io.Copy treats n < len(p) with a nil
	// error as its own bug and returns "short write" -- which cmd.Run()
	// surfaces as a process failure even though the process ran and answered
	// correctly. So the ORIGINAL length is always reported; the buffer keeps
	// only the HEAD of the output up to limit and silently drops the rest.
	return n, nil
}

func (b *postureBoundedBuffer) String() string { return b.buf.String() }

// probeFunc answers one check. selector is the SIGNED string from the policy
// and is never a command, a script or a path the server composed.
type probeFunc func(ctx context.Context, run postureRunner, selector string) Sample

// boolSample is the ONLY way this package produces a definite posture answer.
// Every other path returns unavailable(), which is what makes the asymmetry in
// §5 of the design a property of the code rather than of each author's care.
func boolSample(v bool) Sample { return Sample{Available: true, Bool: v} }

type postureSampler struct {
	run    postureRunner
	probes map[string]probeFunc
	mu     sync.Mutex

	// The pass budget's state. Guarded by mu, so it is read and written only by
	// the single caller that holds the pass.
	//
	// passBudget is a field rather than the constant read directly so a test
	// can shorten it to milliseconds; nothing in the agent changes it.
	passBudget   time.Duration
	passDeadline time.Time // when the pass in flight must stop; zero == no pass yet
	lastReturn   time.Time // when the previous Sample call returned
}

func newPostureSampler(run postureRunner, probes map[string]probeFunc) Sampler {
	return &postureSampler{run: run, probes: probes, passBudget: posturePassBudget}
}

// NewPostureSampler returns the platform's posture-cadence sampler.
func NewPostureSampler() Sampler { return newPlatformPosture() }

func (p *postureSampler) Sample(checkType, selector string) Sample {
	probe, ok := p.probes[checkType]
	if !ok {
		// Includes every check this platform cannot answer: they are simply
		// absent from the table. §0.7 -- unavailable is stored, surfaced and
		// countable, and never opens or clears an alert.
		return unavailable()
	}
	// TryLock, not Lock: a caller that finds a pass in flight must be answered
	// now with `unavailable`, not queued behind a second set of shell-outs.
	//
	// # What this guard does NOT defend against, corrected 2026-08-13
	//
	// It was justified as keeping `health.check.now` from landing on top of a
	// posture sub-tick, on the grounds that only one of the two is a job so the
	// Dispatcher's per-capability serialisation does not separate them. That
	// reason is FALSE: Engine.onTick and Engine.CheckNow both hold `e.mu`
	// across their entire sample loop, so those two callers cannot reach this
	// object concurrently and this guard never fires by that route. The engine
	// mutex is what serialises them -- and it is also why a slow pass stalls
	// the wheel, which is what the pass budget above is for.
	//
	// What it actually defends against is any OTHER caller of a shared sampler:
	// a second Engine over one NewPostureSampler(), a job path that samples
	// without the engine, a test. It is kept because it is one atomic operation
	// on the uncontended path and the failure it prevents -- two overlapping
	// sets of shell-outs, in a package whose whole footprint claim is about not
	// spawning things -- is expensive and silent.
	//
	// It is per SAMPLE CALL, not per pass. A losing caller therefore loses only
	// the checks that overlap: it can be refused check 1 and win check 2, and
	// report a mixture of `unavailable` and real answers. That is stated rather
	// than fixed because every value in such a mixture was still measured by
	// the caller that got it -- §5's rule is about inventing answers, and this
	// invents none -- and because making it per-pass would need a pass identity
	// the Sampler interface has no way to pass in.
	if !p.mu.TryLock() {
		return unavailable()
	}
	defer p.mu.Unlock()

	now := time.Now()
	// A new pass starts when this call is the first one, or when the previous
	// one returned longer than posturePassGap ago. See that constant for why a
	// gap is a sound boundary here and why the Sampler interface cannot carry
	// one instead.
	if p.passDeadline.IsZero() || now.Sub(p.lastReturn) > posturePassGap {
		p.passDeadline = now.Add(p.passBudget)
	}
	// Runs before the Unlock deferred above it (LIFO), so lastReturn is still
	// written under the lock.
	defer func() { p.lastReturn = time.Now() }()

	if !now.Before(p.passDeadline) {
		// The budget for this pass is spent. The remaining checks report
		// `unavailable` -- a real, countable state that never opens or clears
		// an alert (§0.7) -- and no command is run for them. They never report
		// a definite answer: this is the one place where a check is skipped
		// rather than measured, and a `false` invented here would be a breach
		// alert nothing observed.
		return unavailable()
	}
	ctx, cancel := context.WithDeadline(context.Background(), p.passDeadline)
	defer cancel()
	return probe(ctx, p.run, selector)
}

package inventory

import (
	"bytes"
	"context"
	"os/exec"
	"time"
)

// Shell-outs.
//
// Three rules, and inventory_test.go's source walker enforces the first of
// them:
//
//  1. **Nothing job-supplied ever reaches an argument.** InventoryCollectParams
//     is empty and this package never calls job.Params(), so every argument
//     below is a compile-time constant or a value read from the OS itself.
//  2. **No shell.** exec.CommandContext with an argv, never `sh -c` and never
//     `cmd /c` with a composed string, so there is no word splitting and no
//     metacharacter to escape.
//  3. **Bounded and job-scoped.** Output is capped, the context is the job's,
//     and the process is gone before the capability returns. Nothing here runs
//     when the agent is idle.

// maxCommandOutput caps what one command may contribute. A hostile or broken
// tool that writes forever costs a bounded buffer rather than the agent's
// memory budget.
//
// THE BOUND IS ENFORCED WHILE COLLECTING, NOT AFTERWARDS, and that distinction
// is the whole value of this constant. The original implementation set
// cmd.Stdout to a plain bytes.Buffer and truncated the result once the process
// had exited, which capped the RETURNED STRING and nothing else: a tool that
// wrote 64 MiB cost 335,577,488 bytes of allocation before a single byte was
// discarded. budget_test.go's falsifier 2 measures this and is what caught it;
// exec_test.go's TestRunCapsWhatOneToolMayContribute could not, because it
// asserts on the length of the returned string, which was correct all along.
const maxCommandOutput = 1 << 20 // 1 MiB

// waitDelay bounds the wait for a killed tool's pipes to close; see run.
const waitDelay = 2 * time.Second

// capWriter keeps at most limit bytes and silently drops the rest.
//
// Write REPORTS THE FULL LENGTH AS WRITTEN even when it kept none of it, and
// that is deliberate: os/exec's output copier treats a short write as an error,
// abandons the copy and surfaces it from Wait, which would turn "this tool is
// chatty" into "this tool failed" and discard the megabyte already collected.
// Reporting the full length lets the child run to completion and be reaped
// normally while this process holds a fixed buffer.
type capWriter struct {
	buf   bytes.Buffer
	limit int
}

func (w *capWriter) Write(p []byte) (int, error) {
	if room := w.limit - w.buf.Len(); room > 0 {
		if len(p) > room {
			w.buf.Write(p[:room])
		} else {
			w.buf.Write(p)
		}
	}
	return len(p), nil
}

// run executes argv and returns its stdout, or "" if anything went wrong.
//
// Absence is ordinary: `ip` is not installed in every container, `powershell`
// is absent from Windows Nano, and `ioreg` needs no explanation on a machine
// that is not a Mac. Every caller treats "" as "the OS did not say", which is
// the honest answer and never a guess.
func run(ctx context.Context, name string, args ...string) string {
	path, err := exec.LookPath(name)
	if err != nil {
		return ""
	}
	cmd := exec.CommandContext(ctx, path, args...)
	// No stdin: a tool that decides to prompt gets EOF instead of hanging until
	// the job's deadline.
	cmd.Stdin = nil
	// WaitDelay is not optional here, and this was MEASURED rather than
	// reasoned about. Cancelling the context kills the process, but Wait then
	// blocks until the stdout pipe closes -- and a grandchild that inherited
	// that pipe keeps it open. `run(ctx, "sh", "-c", "sleep 30")` under a 200ms
	// deadline took 30.003s before this line existed
	// (TestRunIsBoundedByTheJobsContext, which is why that test asserts on
	// elapsed time rather than on the returned error).
	//
	// On an endpoint that means a shell-out could hold a capability open past
	// the job's own expires_at, reporting a result for a job the control plane
	// has already written off.
	cmd.WaitDelay = waitDelay
	// Bounded AS IT ARRIVES. See capWriter: the previous version buffered
	// everything the tool wrote and truncated afterwards, which capped the
	// answer without capping the footprint.
	out := &capWriter{limit: maxCommandOutput}
	cmd.Stdout = out
	if err := cmd.Run(); err != nil && out.buf.Len() == 0 {
		return ""
	}
	return out.buf.String()
}

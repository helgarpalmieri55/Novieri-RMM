package inventory

import (
	"context"
	"os/exec"
	"strings"
	"testing"
	"time"
)

// The shell-out helper. Every per-OS gatherer that runs a tool goes through
// this one function, so its degradation rules are asserted once, here, on the
// platform this container actually has.

func TestRunReturnsWhatTheToolPrinted(t *testing.T) {
	requireCommand(t, "echo")
	if got := strings.TrimSpace(run(context.Background(), "echo", "hello")); got != "hello" {
		t.Fatalf("run(echo hello) = %q", got)
	}
}

func TestRunTreatsAMissingToolAsSilenceNotAsAnError(t *testing.T) {
	// `ip` is absent from this very container, `powershell.exe` is absent from
	// Windows Nano, `ioreg` is absent from anything that is not a Mac. Every
	// caller reads "" as "the OS did not say", which becomes an omitted fact or
	// an `unknown` origin -- never a guess.
	//
	// FALSIFIER: a non-empty string, or a panic, for a command that is not
	// installed.
	if got := run(context.Background(), "definitely-not-installed-anywhere", "--version"); got != "" {
		t.Fatalf("run(missing tool) = %q; want the empty string", got)
	}
}

func TestRunKeepsOutputFromAToolThatAlsoFailed(t *testing.T) {
	// A non-zero exit with usable stdout is common (`sw_vers` on an odd
	// release, `ip` warning about a namespace). Discarding the output because
	// the exit code was not zero would throw away the answer.
	requireCommand(t, "sh")
	out := run(context.Background(), "sh", "-c", "echo partial; exit 3")
	if strings.TrimSpace(out) != "partial" {
		t.Fatalf("run = %q; stdout must survive a non-zero exit", out)
	}
}

func TestRunIsBoundedByTheJobsContext(t *testing.T) {
	// The context here is the JOB's: a tool that hangs must not hold the
	// capability open until the job expires. FALSIFIER: an elapsed time near
	// the sleep rather than near the deadline.
	requireCommand(t, "sh")
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	started := time.Now()
	_ = run(ctx, "sh", "-c", "sleep 30")
	if elapsed := time.Since(started); elapsed > 5*time.Second {
		t.Fatalf("run took %s; the context deadline was 200ms", elapsed)
	}
}

func TestRunCapsWhatOneToolMayContribute(t *testing.T) {
	// FALSIFIER: a returned length above the cap. A tool that writes forever
	// costs a bounded buffer, not the agent's memory budget.
	requireCommand(t, "sh")
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	out := run(ctx, "sh", "-c", "yes 0123456789abcdef | head -c 4000000")
	if len(out) > maxCommandOutput {
		t.Fatalf("run returned %d bytes; the cap is %d", len(out), maxCommandOutput)
	}
	if len(out) == 0 {
		t.Skip("this shell produced nothing; the cap assertion above would be vacuous")
	}
}

func requireCommand(t *testing.T, name string) {
	t.Helper()
	if _, err := exec.LookPath(name); err != nil {
		t.Skipf("%s is not installed in this container", name)
	}
}

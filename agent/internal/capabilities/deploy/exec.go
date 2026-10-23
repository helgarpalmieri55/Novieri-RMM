package deploy

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os/exec"
	"strings"
	"sync"
	"time"
	"unicode"
)

// THE ONE PLACE THIS PACKAGE STARTS A PROCESS.
//
// Everything above it — hash verification, output truncation, exit-code
// mapping, timeout arithmetic, refusal classification — is pure logic that
// runs identically on every platform and is unit-tested on Linux. Everything
// below it is a handful of lines that hand an already-planned argv to the
// kernel. That split is deliberate: the CI container has no MSI, no .pkg, no
// winget and no PowerShell, so the parts that cannot be exercised here are
// made as thin as they can be and are marked as such.

// Command is a fully resolved process: an absolute program path and an argv
// tail in which every element is either a compile-time constant, a path this
// agent created, or a token that passed args.go's allowlist.
//
// It is a struct rather than a []string so that "the program" and "its
// arguments" cannot be confused for one another, and so a reviewer can see at
// a glance that nothing here is a command LINE.
type Command struct {
	// Path is the absolute path of the program, as resolved by LookPath or as
	// an artifact this agent wrote itself. Never a bare name: resolving at
	// spawn time would mean a PATH entry decides which binary runs as SYSTEM.
	Path string
	// Args is argv[1:]. Never a shell word list.
	Args []string
	// Dir is the working directory, which is always the job's own scratch
	// directory. An installer that writes relative paths writes them somewhere
	// that is removed when the job ends.
	Dir string
	// Label is what this process is called in a log record. It is this
	// package's own word (e.g. "powershell", "msiexec"), never a job-supplied
	// string, and it exists so a log line can say what ran without naming a
	// path that might carry an operator's directory layout.
	Label string
}

// Outcome is what one spawned process did.
type Outcome struct {
	// ExitCode is the process's exit status. Meaningful only when Started.
	ExitCode int
	// Output is the combined stdout+stderr, already capped and sanitised.
	Output string
	// Truncated reports that Output is the TAIL of a larger stream.
	Truncated bool
	// TimedOut reports that the context deadline ended the process rather than
	// the process ending itself.
	TimedOut bool
}

// Executor spawns one Command. Production is spawn; tests supply a fake so the
// logic above this line is fully exercised on a machine that has none of the
// programs plan.go names.
//
// An Executor returns an error ONLY when the process could not be started or
// could not be waited for. A process that started and exited non-zero is a
// successful Executor call with a non-zero Outcome.ExitCode — the difference
// between "the installer failed" and "there was no installer" is exactly the
// difference between a job result and a job error, and folding them together
// is how a missing binary comes to look like a failed install.
type Executor func(ctx context.Context, cmd Command) (Outcome, error)

// maxCapture is the most output one process may contribute to memory. An
// installer that writes a gigabyte to stdout costs a bounded ring, not the
// agent's whole memory budget.
//
// This is the CAPTURE bound. maxLogChars below is the smaller bound on what
// reaches the job result, because a megabyte of installer chatter in a
// `job_result` frame is the control plane's problem rather than the agent's.
const maxCapture = 1 << 20 // 1 MiB

// maxLogChars is what `InstallOutcome.log` may carry. The spec's own words:
// "log (str, truncated; full log uploaded as an artifact and referenced by
// log_ref)". There is no agent-facing artifact-upload route, so `log_ref` is
// always null and this is all the operator gets — see the note in install.go.
const maxLogChars = 16 << 10 // 16 KiB

// waitDelay bounds the wait AFTER the context has killed the process.
//
// exec.CommandContext DOES NOT BOUND A SHELL-OUT, and that was measured on
// this project rather than reasoned about: cancelling the context kills the
// child, but Wait then blocks until the stdout pipe closes, and a grandchild
// that inherited the pipe holds it open. internal/capabilities/inventory's
// exec.go records `sh -c "sleep 30"` under a 200 ms deadline taking 30.003 s
// before WaitDelay existed.
//
// An installer is the worst case for this. MSI bootstrappers routinely spawn
// msiexec children and exit; a `choco` run spawns PowerShell which spawns
// everything else. Without this line a deploy job whose deadline passed could
// hold the capability open long past the job's own expires_at, reporting a
// result for a job the control plane has already written off.
//
// Five seconds rather than inventory's two: an installer that has just been
// killed mid-transaction is entitled to a moment to unwind, and this delay
// only ever runs after the deadline has already passed.
const waitDelay = 5 * time.Second

// spawn is the production Executor. It is deliberately thin — see the note at
// the top of this file.
//
// NOT EXERCISED IN CI FOR ANY REAL INSTALLER. It is exercised for the parts
// that are testable on Linux (a program that exits non-zero, a program that
// floods stdout, a program that ignores its deadline, a program that does not
// exist), because those are the behaviours the logic above depends on.
func spawn(ctx context.Context, c Command) (Outcome, error) {
	// NO SHELL. exec.CommandContext takes an argv, so there is no word
	// splitting, no metacharacter to escape and no interpreter between this
	// process and the program. exec_test.go's source walker fails the build if
	// "sh", "bash -c", "cmd", "cmd.exe" or "/c" ever appears as a spawned
	// program in this package.
	cmd := exec.CommandContext(ctx, c.Path, c.Args...)
	cmd.Dir = c.Dir
	// No stdin: an installer that decides to prompt gets EOF instead of
	// hanging until the job's deadline.
	cmd.Stdin = nil
	cmd.WaitDelay = waitDelay

	buf := &capBuffer{limit: maxCapture}
	cmd.Stdout = buf
	cmd.Stderr = buf

	runErr := cmd.Run()

	out := Outcome{
		Output:    buf.String(),
		Truncated: buf.truncated,
		TimedOut:  ctx.Err() != nil,
	}

	if runErr == nil {
		return out, nil
	}
	var exitErr *exec.ExitError
	if errors.As(runErr, &exitErr) {
		// The process ran and exited non-zero. That is an OUTCOME, not an
		// error: see Executor's doc comment.
		out.ExitCode = exitErr.ExitCode()
		return out, nil
	}
	// Could not start, or could not wait. The program's LABEL is named, never
	// its path or its arguments -- but the KIND of failure is, because "could
	// not be started" alone cost a whole CI round to diagnose.
	return out, fmt.Errorf("%s could not be started (%s)", c.Label, startFailure(runErr))
}

// startFailure classifies why a process did not start, in words of this
// package's own: exec.Error and *PathError both carry the program's path.
func startFailure(err error) string {
	switch {
	case errors.Is(err, exec.ErrNotFound), errors.Is(err, fs.ErrNotExist):
		return "not found"
	case errors.Is(err, fs.ErrPermission):
		return "access denied"
	case errors.Is(err, exec.ErrDot):
		return "resolved from the working directory"
	}
	return "start or wait failed"
}

// capBuffer is a bounded sink that keeps the TAIL.
//
// The tail and not the head, and the choice is not arbitrary: an installer
// that fails says why on its last few lines, and a script that fails does the
// same. Keeping the first 16 KiB of a verbose MSI log would reliably capture
// the banner and reliably lose the error.
//
// It is safe for concurrent writes. os/exec gives stdout and stderr the same
// pipe when they are the same io.Writer, so in practice one goroutine writes
// here — but "in practice" is not a synchronisation argument, and -race would
// be the one to find out.
type capBuffer struct {
	mu        sync.Mutex
	limit     int
	buf       []byte
	truncated bool
}

var _ io.Writer = (*capBuffer)(nil)

func (b *capBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	n := len(p)
	if n > b.limit {
		// One write larger than the whole budget: keep only its tail.
		p = p[n-b.limit:]
		b.truncated = true
	}
	if len(b.buf)+len(p) > b.limit {
		drop := len(b.buf) + len(p) - b.limit
		b.buf = b.buf[drop:]
		b.truncated = true
	}
	b.buf = append(b.buf, p...)
	// The FULL length is reported to the caller. Reporting a short write makes
	// os/exec treat the cap as an I/O error and abandon the process's output
	// entirely, which turns "the installer was chatty" into "the installer
	// produced nothing".
	return n, nil
}

func (b *capBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return string(b.buf)
}

// wireLog renders a process's output for `InstallOutcome.log`.
//
// Two jobs, and both of them are about the operator's console rather than
// about the agent:
//
//   - BOUNDED. The tail of at most maxLogChars, with an explicit marker when
//     bytes were dropped, so nobody reads a truncated log as a complete one.
//   - PRINTABLE. Stripped to printable ASCII plus newline and tab, exactly as
//     internal/jobs' sanitize does for refusal reasons. An installer that
//     emits bidi overrides or ANSI escapes into a console where one job's
//     record is read next to another's is a way to make one record read as
//     another, and the bytes come from a program the operator chose but did
//     not write.
//
// THE ORDER OF THESE TWO STEPS IS A FOOTPRINT DECISION, and it was made by a
// measurement rather than by taste. Sanitising first and truncating afterwards
// builds a copy of the WHOLE stream: budget_test.go read 104,876,080 bytes per
// call for a 100 MiB input against a 16 KiB wire cap. Truncating first bounds
// the copy at the cap. `spawn` already caps capture at 1 MiB so production
// never sees 100 MiB here — but `wireLog` is a pure function that a future
// caller may hand anything, and a bound that depends on every caller being
// careful is not a bound.
//
// Truncating raw bytes can cut a multi-byte rune in half. That is harmless
// here precisely because sanitizeLog runs afterwards: an invalid byte is not
// printable ASCII, so it becomes `?` like any other.
func wireLog(out Outcome) string {
	raw := out.Output
	dropped := out.Truncated
	if len(raw) > maxLogChars {
		raw = raw[len(raw)-maxLogChars:]
		dropped = true
	}
	// sanitizeLog never lengthens: it drops \r and maps each non-printable or
	// non-ASCII RUNE to a single byte, so the result is at most maxLogChars.
	s := sanitizeLog(raw)
	if dropped {
		return "[output truncated; the tail is shown]\n" + s
	}
	return s
}

func sanitizeLog(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		switch {
		case r == '\n' || r == '\t':
			b.WriteRune(r)
		case r == '\r':
			// Swallowed rather than replaced: Windows tools emit CRLF and a
			// console full of `?` line endings is worse than none.
		case r > unicode.MaxASCII || !unicode.IsPrint(r):
			b.WriteByte('?')
		default:
			b.WriteRune(r)
		}
	}
	return b.String()
}

// clampIdentifier bounds and sanitises a string that will be echoed into a job
// result. Same treatment as a log line, and for the same reason.
func clampIdentifier(s string) string {
	s = sanitizeLog(s)
	s = strings.ReplaceAll(s, "\n", " ")
	s = strings.ReplaceAll(s, "\t", " ")
	if len(s) > maxIdentifier {
		s = s[:maxIdentifier]
	}
	return s
}

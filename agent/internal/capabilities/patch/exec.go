package patch

import (
	"bytes"
	"context"
	"errors"
	"os/exec"
	"time"
)

// Shell-outs, and the four rules that govern every one of them.
//
//  1. **NOTHING JOB-SUPPLIED EVER REACHES AN ARGUMENT.** Every argv element
//     below is either a compile-time constant or a value the OS itself
//     produced and this package then matched for equality. See engine.go for
//     why that is structural.
//  2. **No shell.** exec.CommandContext with an argv -- never `sh -c`, never
//     `cmd /c` with a composed string. There is no word splitting and no
//     metacharacter to escape, because there is no parser downstream.
//  3. **The narrowest binary that can do the work**, resolved by absolute path
//     where one exists. On Windows this file is never reached at all: the
//     Windows engine is in-process COM precisely so that a SYSTEM process does
//     not spawn `powershell.exe`, which is among the loudest behavioural
//     signals an EDR watches for and trips ASR rules outright.
//  4. **Bounded, and gone before the capability returns.**

// maxCommandOutput caps what one command may contribute. `apt-get -s upgrade`
// on a machine that has not been updated in a year is tens of kilobytes; a
// hostile or broken tool that writes forever costs a bounded buffer rather than
// the agent's whole memory budget.
const maxCommandOutput = 4 << 20 // 4 MiB

// commandResult is one completed shell-out.
//
// Stdout and Stderr are kept separately because they answer different
// questions: apt-get writes its plan to stdout and its complaints to stderr,
// and folding them together would put "Reading package lists..." into an
// operator's `last_error`.
type commandResult struct {
	Stdout   string
	Stderr   string
	ExitCode int
	// Err is set when the command could not be run or did not exit cleanly.
	// A non-zero exit is BOTH a non-zero ExitCode and a non-nil Err.
	Err error
}

// ok reports whether the command ran and exited zero.
func (r commandResult) ok() bool { return r.Err == nil }

// runner is the injected seam every shell-out engine takes.
//
// It exists so apt's and softwareupdate's parsing, filtering and outcome
// mapping are unit-tested against recorded output on a machine that has
// neither, and so a test can assert WHAT WAS NOT RUN -- which is the assertion
// that matters for "no job-supplied string reaches an argument".
//
// The environment is part of the seam rather than ambient, because it is part
// of what the command DOES: `DEBIAN_FRONTEND=noninteractive` is the difference
// between an install that completes and one that sits on a debconf prompt until
// the job's deadline, and a test must be able to assert it was set.
type runner func(ctx context.Context, env []string, name string, args ...string) commandResult

// execRunner is the production runner. It runs argv, optionally with extra
// environment, and returns everything a caller needs to classify the result.
func execRunner(ctx context.Context, extraEnv []string, name string, args ...string) commandResult {
	path, err := exec.LookPath(name)
	if err != nil {
		return commandResult{ExitCode: -1, Err: err}
	}

	cmd := exec.CommandContext(ctx, path, args...)
	// No stdin. An installer that decides to prompt gets EOF instead of hanging
	// until the job's deadline -- and `apt-get` genuinely does prompt about
	// modified conffiles when it thinks it has a terminal.
	cmd.Stdin = nil
	if len(extraEnv) > 0 {
		cmd.Env = append(cmd.Environ(), extraEnv...)
	}

	// WaitDelay is NOT OPTIONAL, and this was measured on this project rather
	// than reasoned about (internal/capabilities/inventory/exec.go records the
	// same finding). exec.CommandContext does not bound a shell-out: cancelling
	// the context kills the direct child, but Wait then blocks until the stdout
	// pipe closes, and a GRANDCHILD that inherited that pipe keeps it open.
	// `sh -c "sleep 30"` under a 200 ms deadline took 30.003 s before this line
	// existed.
	//
	// That matters more here than anywhere else in the agent. `apt-get install`
	// forks dpkg, which forks maintainer scripts, which fork whatever they
	// like; a package whose postinst starts a daemon holds the pipe open for as
	// long as the daemon runs. Without this, one such package would hold a
	// patch job open past its own expires_at and report a result for a job the
	// control plane has already written off.
	//
	// The delay is generous compared with inventory's 2 s because a killed
	// installer deserves a moment to unwind dpkg's database, and because this
	// path is not on any latency budget.
	cmd.WaitDelay = 10 * time.Second

	var stdout, stderr boundedBuffer
	stdout.limit, stderr.limit = maxCommandOutput, maxCommandOutput
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	runErr := cmd.Run()
	res := commandResult{Stdout: stdout.String(), Stderr: stderr.String(), Err: runErr}
	var exitErr *exec.ExitError
	switch {
	case runErr == nil:
		res.ExitCode = 0
	case errors.As(runErr, &exitErr):
		res.ExitCode = exitErr.ExitCode()
	default:
		res.ExitCode = -1
	}
	return res
}

// boundedBuffer is an io.Writer that stops accepting after limit bytes.
//
// A cap enforced by truncating AFTERWARDS is not a cap: the memory was already
// allocated. This one refuses the write, so a tool that decides to print
// forever costs a fixed 4 MiB and not the agent's residency claim.
type boundedBuffer struct {
	buf   bytes.Buffer
	limit int
}

func (b *boundedBuffer) Write(p []byte) (int, error) {
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
	// correctly. That turns a verbose but successful apt/dnf run into a
	// failed patch job. So the ORIGINAL length is always reported; the buffer
	// keeps only the HEAD of the output up to limit and silently drops the
	// rest.
	return n, nil
}

func (b *boundedBuffer) String() string { return b.buf.String() }

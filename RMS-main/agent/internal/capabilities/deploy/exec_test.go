package deploy

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// THE PRODUCTION SPAWN, EXERCISED FOR WHAT LINUX CAN EXERCISE.
//
// There is no MSI here, no .pkg, no winget and no PowerShell. What CAN be
// driven through the real `spawn` is every behaviour the logic above it
// depends on: a process that exits non-zero, a process that floods stdout, a
// process that ignores its deadline, and a program that does not exist. Those
// are the four, and they are driven through the same function a Windows
// endpoint will use.

// helper builds a small program in a temp directory. Compiling a Go program is
// slower than writing a shell script and is the right trade: a shell script
// would need a shell, and the whole point of this package is that it does not
// spawn one.
func helper(t *testing.T, name, src string) string {
	t.Helper()
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("no go toolchain to build a helper program with")
	}
	dir := t.TempDir()
	srcPath := filepath.Join(dir, "main.go")
	if err := os.WriteFile(srcPath, []byte(src), 0o600); err != nil {
		t.Fatal(err)
	}
	// Windows runs a program by extension: CreateProcess via os/exec will not
	// start an extensionless file, and `go build -o` names the file exactly.
	bin := filepath.Join(dir, name)
	if runtime.GOOS == "windows" {
		bin += ".exe"
	}
	build := exec.Command("go", "build", "-o", bin, srcPath)
	build.Env = append(os.Environ(), "GOFLAGS=")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("building the helper failed: %v\n%s", err, out)
	}
	return bin
}

func TestSpawnReportsANonZeroExitAsAnOutcomeAndNotAsAnError(t *testing.T) {
	// The distinction Executor's doc comment is about: "the installer failed"
	// and "there was no installer" are different facts, and folding them
	// together is how a missing binary comes to look like a failed install.
	//
	// FALSIFYING OBSERVATION: an error returned for a process that ran and
	// exited 17, or an exit code of 0 reported for it.
	bin := helper(t, "exit17", `package main

import "os"

func main() { os.Stdout.WriteString("some output\n"); os.Exit(17) }
`)
	out, err := spawn(context.Background(), Command{Path: bin, Label: "helper"})
	if err != nil {
		t.Fatalf("a process that ran and exited 17 returned an error: %v", err)
	}
	if out.ExitCode != 17 {
		t.Fatalf("exit code = %d, want 17", out.ExitCode)
	}
	if !strings.Contains(out.Output, "some output") {
		t.Fatalf("output = %q; a failing installer's output is the only thing an operator has",
			out.Output)
	}
}

func TestSpawnReportsAProgramThatDoesNotExistAsAnErrorAndNamesOnlyTheLabel(t *testing.T) {
	// The other side. And the message must carry the LABEL, not the path:
	// this error reaches a job result, and a path can carry an operator's
	// directory layout.
	out, err := spawn(context.Background(), Command{
		Path:  filepath.Join(t.TempDir(), "no-such-program"),
		Label: "msiexec",
	})
	if err == nil {
		t.Fatalf("a nonexistent program produced no error (outcome %+v)", out)
	}
	if !strings.Contains(err.Error(), "msiexec") {
		t.Errorf("the error %q does not name the program's label", err)
	}
	if !strings.Contains(err.Error(), "not found") {
		t.Errorf("the error %q does not say why the program did not start", err)
	}
	if strings.Contains(err.Error(), t.TempDir()) {
		t.Errorf("the error %q carries a filesystem path", err)
	}
}

func TestAProcessThatFloodsStdoutCostsABoundedBuffer(t *testing.T) {
	// "An installer writing a gigabyte to stdout must not become a gigabyte in
	// a job result." This writes 64 MiB — enough that an unbounded buffer is
	// unmistakable — and asserts what is retained.
	//
	// FALSIFYING OBSERVATION: captured output larger than maxCapture, or a
	// short-write error that abandoned the process's output entirely.
	bin := helper(t, "flood", `package main

import "os"

func main() {
	chunk := make([]byte, 1<<16)
	for i := range chunk {
		chunk[i] = 'A'
	}
	for i := 0; i < 1024; i++ {
		os.Stdout.Write(chunk)
	}
	os.Stdout.WriteString("TAIL-MARKER")
}
`)
	out, err := spawn(context.Background(), Command{Path: bin, Label: "helper"})
	if err != nil {
		t.Fatalf("a chatty process returned an error: %v. capBuffer.Write must report the FULL "+
			"length or os/exec treats the cap as an I/O failure and abandons the output", err)
	}
	if len(out.Output) > maxCapture {
		t.Fatalf("captured %d bytes from a 64 MiB stream; the cap is %d",
			len(out.Output), maxCapture)
	}
	if !out.Truncated {
		t.Fatal("a truncated capture is not marked as truncated")
	}
	if !strings.HasSuffix(out.Output, "TAIL-MARKER") {
		t.Fatal("the capture kept the head and lost the tail; an installer that fails says why " +
			"on its last few lines")
	}
	// And the wire log is smaller again.
	if got := wireLog(out); len(got) > maxLogChars+64 {
		t.Fatalf("wireLog returned %d characters; the wire cap is %d", len(got), maxLogChars)
	}
}

func TestAProcessThatIgnoresItsDeadlineIsStillBoundedByWaitDelay(t *testing.T) {
	// exec.CommandContext DOES NOT BOUND A SHELL-OUT, and this is the
	// measurement rather than the claim. Cancelling the context kills the
	// child, but Wait blocks until the stdout pipe closes — and a GRANDCHILD
	// that inherited the pipe holds it open. internal/capabilities/inventory
	// recorded `sh -c "sleep 30"` under a 200 ms deadline taking 30.003 s
	// before WaitDelay existed.
	//
	// A long-running installer that spawns a child and exits is exactly this
	// case: MSI bootstrappers do it routinely, and `choco` spawns PowerShell
	// which spawns everything else.
	//
	// FALSIFYING OBSERVATION: elapsed time approaching the grandchild's
	// lifetime rather than the deadline plus waitDelay. Asserted on ELAPSED
	// TIME and not on the returned error, for the same reason inventory's
	// equivalent is.
	if runtime.GOOS == "windows" {
		t.Skip("the grandchild-holds-the-pipe shape is POSIX")
	}
	bin := helper(t, "orphan", `package main

import (
	"os"
	"os/exec"
	"time"
)

func main() {
	// A grandchild that inherits stdout and outlives its parent. This is the
	// shape that makes cmd.Wait block past the context deadline.
	child := exec.Command(os.Args[0], "child")
	child.Stdout = os.Stdout
	if len(os.Args) > 1 {
		time.Sleep(30 * time.Second)
		return
	}
	_ = child.Start()
	time.Sleep(30 * time.Second)
}
`)
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()

	start := time.Now()
	_, _ = spawn(ctx, Command{Path: bin, Label: "helper"})
	elapsed := time.Since(start)

	// The deadline plus WaitDelay plus slack. Well under the 30 s the
	// grandchild sleeps for, which is the number this is guarding against.
	limit := 200*time.Millisecond + waitDelay + 3*time.Second
	if elapsed > limit {
		t.Fatalf("spawn took %v under a 200ms deadline (limit %v). cmd.WaitDelay is what bounds "+
			"this; without it a killed installer holds the capability open past the job's own "+
			"expires_at", elapsed, limit)
	}
	t.Logf("a process whose grandchild holds the pipe: deadline 200ms, spawn returned after %v "+
		"(WaitDelay %v)", elapsed, waitDelay)
}

func TestSpawnGivesTheProcessNoStdin(t *testing.T) {
	// An installer that decides to prompt must get EOF rather than hang until
	// the job's deadline.
	bin := helper(t, "readstdin", `package main

import (
	"io"
	"os"
)

func main() {
	b, err := io.ReadAll(os.Stdin)
	if err != nil {
		os.Exit(2)
	}
	if len(b) != 0 {
		os.Exit(3)
	}
	os.Stdout.WriteString("got EOF")
}
`)
	done := make(chan Outcome, 1)
	go func() {
		out, _ := spawn(context.Background(), Command{Path: bin, Label: "helper"})
		done <- out
	}()
	select {
	case out := <-done:
		if out.ExitCode != 0 || !strings.Contains(out.Output, "got EOF") {
			t.Fatalf("outcome = %+v; a process reading stdin should see EOF immediately", out)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("a process that read stdin hung; cmd.Stdin must be nil")
	}
}

func TestSpawnRunsInTheJobsScratchDirectory(t *testing.T) {
	// An installer that writes relative paths must write them somewhere that
	// is removed when the job ends.
	dir := t.TempDir()
	bin := helper(t, "pwd", `package main

import "os"

func main() {
	wd, _ := os.Getwd()
	os.Stdout.WriteString(wd)
}
`)
	out, err := spawn(context.Background(), Command{Path: bin, Dir: dir, Label: "helper"})
	if err != nil {
		t.Fatal(err)
	}
	// macOS reports /private/var for /var; compare the resolved paths.
	want, _ := filepath.EvalSymlinks(dir)
	got, _ := filepath.EvalSymlinks(strings.TrimSpace(out.Output))
	if got != want {
		t.Fatalf("the process ran in %q, not the job's scratch directory %q", got, want)
	}
}

// ---------------------------------------------------------------------------
// The end-to-end path, through the REAL spawn
// ---------------------------------------------------------------------------

func TestARealScriptRunsEndToEndThroughTheRealExecutor(t *testing.T) {
	// Everything else in this package injects an Executor. This one does not:
	// the params go in, a real process comes out, and the result is a
	// ScriptRunResult. Linux/bash only — it is the one interpreter this
	// container has.
	//
	// FALSIFYING OBSERVATION: a state, exit code or log that does not match
	// what the script actually did.
	if runtime.GOOS == "windows" {
		t.Skip("bash")
	}
	if _, err := exec.LookPath("bash"); err != nil {
		t.Skip("no bash")
	}
	svc := New(Options{Logger: quiet(), ScratchRoot: t.TempDir()})

	res, err := svc.runScript(context.Background(),
		scriptParamsJSON(t, "echo GOOD-OUTPUT\nexit 0\n", nil))
	if err != nil {
		t.Fatalf("runScript: %v", err)
	}
	if res.Outcome.State != stateSucceeded {
		t.Fatalf("state = %q, log = %q", res.Outcome.State, res.Outcome.Log)
	}
	if !strings.Contains(res.Outcome.Log, "GOOD-OUTPUT") {
		t.Fatalf("log = %q; the script's stdout is the whole point", res.Outcome.Log)
	}

	res, err = svc.runScript(context.Background(),
		scriptParamsJSON(t, "echo BAD >&2\nexit 42\n", nil))
	if err != nil {
		t.Fatalf("runScript: %v", err)
	}
	if res.Outcome.State != stateFailed {
		t.Fatalf("state = %q for a script that exited 42", res.Outcome.State)
	}
	if res.Outcome.ExitCode == nil || *res.Outcome.ExitCode != 42 {
		t.Fatalf("exit code = %v, want 42", res.Outcome.ExitCode)
	}
	if !strings.Contains(res.Outcome.Log, "BAD") {
		t.Fatalf("log = %q; stderr must be captured too, it is where the error is",
			res.Outcome.Log)
	}
	if svc.Scripts() != 0 {
		t.Fatal("runScript incremented the counter; only the Run handler should")
	}
}

func TestTheScriptRunTimeoutBoundsARealProcess(t *testing.T) {
	// `timeout_s` is the job's own bound on top of the job deadline, and it
	// has to bound a REAL process or it is decoration.
	if runtime.GOOS == "windows" {
		t.Skip("bash")
	}
	if _, err := exec.LookPath("bash"); err != nil {
		t.Skip("no bash")
	}
	svc := New(Options{Logger: quiet(), ScratchRoot: t.TempDir()})
	start := time.Now()
	res, err := svc.runScript(context.Background(),
		scriptParamsJSON(t, "sleep 30\n", map[string]any{"timeout_s": 1}))
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("runScript: %v", err)
	}
	if elapsed > 1*time.Second+waitDelay+3*time.Second {
		t.Fatalf("a 1-second timeout took %v", elapsed)
	}
	if res.Outcome.State != stateFailed {
		t.Fatalf("a script killed at its timeout reported %q", res.Outcome.State)
	}
	if !strings.Contains(res.Outcome.Log, "deadline passed") {
		t.Fatalf("the log does not say the process was killed: %q", res.Outcome.Log)
	}
	t.Logf("timeout_s=1 against `sleep 30`: returned after %v", elapsed)
}

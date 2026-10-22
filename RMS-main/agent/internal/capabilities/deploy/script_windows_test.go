//go:build windows

package deploy

import (
	"context"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/windows"
)

// script.run's whole Windows path through the REAL pieces: the restricted
// scratch directory, the .ps1 on disk, the PowerShell the planner resolves and
// the real spawn. exec_test.go's bash twins cannot run here, and every other
// test in this package injects an Executor.
//
// FALSIFYING OBSERVATION: any state, exit code or log that does not match what
// the script did, or a process that could not be started at all.

func requireElevated(t *testing.T) {
	t.Helper()
	// The scratch DACL grants SYSTEM and Administrators only; a filtered
	// (non-elevated) admin token cannot write the script into it.
	if !windows.GetCurrentProcessToken().IsElevated() {
		t.Skip("the scratch directory admits SYSTEM and elevated Administrators only; " +
			"GitHub's windows-latest runner is elevated, so CI runs this")
	}
}

func runPS(t *testing.T, svc *Service, body string, extra map[string]any) installOutcome {
	t.Helper()
	params := map[string]any{"interpreter": "powershell"}
	for k, v := range extra {
		params[k] = v
	}
	res, err := svc.runScript(context.Background(), scriptParamsJSON(t, body, params))
	if err != nil {
		t.Fatalf("runScript: %v", err)
	}
	return res.Outcome
}

func TestARealPowerShellScriptRunsEndToEndOnWindows(t *testing.T) {
	requireElevated(t)
	svc := New(Options{Logger: quiet(), ScratchRoot: t.TempDir()})

	out := runPS(t, svc, "Write-Output 'GOOD-OUTPUT'\r\n"+
		"Write-Output ('cwd=' + (Get-Location).Path)\r\nexit 0\r\n", nil)
	if out.State != stateSucceeded {
		t.Fatalf("state = %q, log = %q", out.State, out.Log)
	}
	if !strings.Contains(out.Log, "GOOD-OUTPUT") {
		t.Fatalf("log = %q; the script's stdout is the whole point", out.Log)
	}
	if !strings.Contains(out.Log, `\its-deploy-`) {
		t.Errorf("log = %q; the script did not run in the job's scratch directory", out.Log)
	}

	out = runPS(t, svc, "[Console]::Error.WriteLine('BAD')\r\nexit 42\r\n", nil)
	if out.State != stateFailed || out.ExitCode == nil || *out.ExitCode != 42 {
		t.Fatalf("state = %q exit = %v for a script that exited 42", out.State, out.ExitCode)
	}
	if !strings.Contains(out.Log, "BAD") {
		t.Fatalf("log = %q; stderr must be captured too, it is where the error is", out.Log)
	}

	// No stdin: a script that reads it gets EOF at once rather than hanging.
	out = runPS(t, svc, "$in = [Console]::In.ReadToEnd()\r\n"+
		"if ($in.Length -ne 0) { exit 3 }\r\nWrite-Output 'got EOF'\r\n", nil)
	if out.State != stateSucceeded || !strings.Contains(out.Log, "got EOF") {
		t.Fatalf("state = %q, log = %q; stdin must be empty", out.State, out.Log)
	}
}

func TestTheScriptRunTimeoutBoundsARealPowerShell(t *testing.T) {
	requireElevated(t)
	svc := New(Options{Logger: quiet(), ScratchRoot: t.TempDir()})
	start := time.Now()
	out := runPS(t, svc, "Start-Sleep -Seconds 30\r\n", map[string]any{"timeout_s": 1})
	elapsed := time.Since(start)
	if elapsed > 1*time.Second+waitDelay+10*time.Second {
		t.Fatalf("a 1-second timeout took %v", elapsed)
	}
	if out.State != stateFailed || !strings.Contains(out.Log, "deadline passed") {
		t.Fatalf("state = %q, log = %q for a script killed at its timeout", out.State, out.Log)
	}
	t.Logf("timeout_s=1 against Start-Sleep 30: returned after %v", elapsed)
}

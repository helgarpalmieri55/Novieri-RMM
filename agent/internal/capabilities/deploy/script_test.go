package deploy

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// The body is what runs, and it runs from a file
// ---------------------------------------------------------------------------

func TestTheSignedBodyIsWhatReachesTheInterpreterAndItGoesViaAFile(t *testing.T) {
	// The body travels inside the signed envelope (AD-02). This asserts the
	// two things that follow: the bytes on disk are the bytes in the params,
	// byte for byte, and the interpreter is pointed at that FILE rather than
	// handed the body on a command line.
	//
	// FALSIFYING OBSERVATION: an argv element that contains any part of the
	// body, or a file whose contents differ from the params.
	const body = "#!/bin/sh\necho hello\nexit 0\n"
	var seen string
	spy := &execSpy{}
	captured := ""
	svc := New(Options{
		GOOS:     "linux",
		LookPath: fakePaths("bash"),
		Logger:   quiet(),
		Exec: func(ctx context.Context, c Command) (Outcome, error) {
			// Read the script file back WHILE the "process" is running, which
			// is the only moment it exists.
			raw, err := os.ReadFile(c.Args[len(c.Args)-1])
			if err != nil {
				t.Errorf("the script file is not readable at execution time: %v", err)
			}
			captured = string(raw)
			seen = strings.Join(append([]string{c.Path}, c.Args...), " ")
			return spy.exec(ctx, c)
		},
	})

	res, err := svc.runScript(context.Background(), scriptParamsJSON(t, body, nil))
	if err != nil {
		t.Fatalf("runScript: %v", err)
	}
	if captured != body {
		t.Fatalf("the file the interpreter read is %q; the signed body is %q. The thing that "+
			"runs must be the thing that was signed", captured, body)
	}
	// The body must not appear ANYWHERE on the command line. `bash -c <body>`
	// would put it in every process listing on the box and in every EDR
	// telemetry event, which is where credentials in a script body leak from.
	for _, line := range strings.Split(body, "\n") {
		if line == "" {
			continue
		}
		if strings.Contains(seen, line) {
			t.Fatalf("the command line %q carries a line of the script body (%q)", seen, line)
		}
	}
	if res.Outcome.State != stateSucceeded {
		t.Fatalf("state = %q, want %q", res.Outcome.State, stateSucceeded)
	}
}

func TestTheScratchDirectoryAndEverythingInItAreGoneWhenTheJobEnds(t *testing.T) {
	// The body is an operator's secret sitting on disk. It must not outlive
	// the job.
	//
	// FALSIFYING OBSERVATION: a file under the scratch root after runScript
	// returns.
	root := t.TempDir()
	var scriptPath string
	svc := New(Options{
		GOOS: "linux", LookPath: fakePaths("bash"), Logger: quiet(), ScratchRoot: root,
		Exec: func(context.Context, Command) (Outcome, error) { return Outcome{}, nil },
	})
	svc.opts.Exec = func(_ context.Context, c Command) (Outcome, error) {
		scriptPath = c.Args[len(c.Args)-1]
		return Outcome{}, nil
	}
	if _, err := svc.runScript(context.Background(), scriptParamsJSON(t, "echo x", nil)); err != nil {
		t.Fatal(err)
	}
	if scriptPath == "" {
		t.Fatal("the executor never saw a script path; this test would be vacuous")
	}
	if _, err := os.Stat(scriptPath); err == nil {
		t.Fatalf("%s still exists after the job ended; a script body must not outlive its job",
			scriptPath)
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("the scratch root still holds %d entries after the job ended", len(entries))
	}
}

func TestTheScratchDirectoryIsOwnerOnly(t *testing.T) {
	// A world-writable parent makes every file inside it replaceable no matter
	// what mode the file has — which is how a hash-verified installer gets
	// swapped for someone else's binary in the window before execution.
	//
	// FALSIFYING OBSERVATION: any permission bit outside 0700 on the directory,
	// or outside 0600 on the script file.
	//
	// POSIX ONLY. The Windows half is perm_windows.go, which sets a protected
	// DACL rather than a POSIX mode bit, so os.Stat().Mode().Perm() on a real
	// Windows filesystem cannot observe 0700/0600 here regardless of what this
	// test's injected GOOS: "linux" made runScript do internally. See
	// perm_windows_test.go for the Windows half's own real-OS check.
	if runtime.GOOS == "windows" {
		t.Skip("POSIX permission bits are not observable via os.Stat on windows: linux/macOS only")
	}
	root := t.TempDir()
	var dir, file string
	svc := New(Options{
		GOOS: "linux", LookPath: fakePaths("bash"), Logger: quiet(), ScratchRoot: root,
		Exec: func(_ context.Context, c Command) (Outcome, error) {
			dir, file = c.Dir, c.Args[len(c.Args)-1]
			di, err := os.Stat(dir)
			if err != nil {
				t.Errorf("stat scratch dir: %v", err)
				return Outcome{}, nil
			}
			if perm := di.Mode().Perm(); perm != 0o700 {
				t.Errorf("the scratch directory is mode %#o, want 0700", perm)
			}
			fi, err := os.Stat(file)
			if err != nil {
				t.Errorf("stat script file: %v", err)
				return Outcome{}, nil
			}
			if perm := fi.Mode().Perm(); perm != 0o600 {
				t.Errorf("the script file is mode %#o, want 0600", perm)
			}
			return Outcome{}, nil
		},
	})
	if _, err := svc.runScript(context.Background(), scriptParamsJSON(t, "echo x", nil)); err != nil {
		t.Fatal(err)
	}
	if dir == "" {
		t.Fatal("the executor never ran; this test would be vacuous")
	}
}

// ---------------------------------------------------------------------------
// Never log a body, never log an argument
// ---------------------------------------------------------------------------

func TestNoLogRecordErrorOrRefusalEverCarriesTheScriptBodyOrItsArguments(t *testing.T) {
	// The rule from CLAUDE.md's invariant #3 applied to the one field the
	// control plane hands this agent that is guaranteed to contain
	// credentials: an operator's script.
	//
	// This drives the SUCCESS path, the REFUSAL path (three different
	// refusals) and the FAILURE path, and searches every log record, every
	// error string and every refusal reason for a distinctive secret planted
	// in the body.
	//
	// FALSIFYING OBSERVATION: the secret in any of them. To prove the search
	// is not vacuous, the same secret IS asserted present in the file the
	// interpreter reads — if the plumbing broke and the body never reached
	// anything, every absence assertion below would pass for the wrong reason.
	const secret = "hunter2-SUPERSECRET-DOMAIN-ADMIN-PASSWORD"
	body := "$cred = '" + secret + "'\nInvoke-Thing -Password $cred\n"

	type probe struct {
		name    string
		params  json.RawMessage
		options func(*Options)
	}
	probes := []probe{
		{
			name:   "success",
			params: scriptParamsJSON(t, body, nil),
		},
		{
			name:   "refused: no interpreter installed",
			params: scriptParamsJSON(t, body, nil),
			options: func(o *Options) {
				o.LookPath = nothingInstalled()
			},
		},
		{
			name:   "refused: unknown interpreter",
			params: scriptParamsJSON(t, body, map[string]any{"interpreter": "ruby"}),
		},
		{
			name:   "refused: timeout out of range",
			params: scriptParamsJSON(t, body, map[string]any{"timeout_s": 0}),
		},
		{
			name:   "failed: the interpreter would not start",
			params: scriptParamsJSON(t, body, nil),
			options: func(o *Options) {
				o.Exec = func(context.Context, Command) (Outcome, error) {
					return Outcome{}, errStartFailed
				}
			},
		},
	}

	sawBodyOnDisk := false
	for _, p := range probes {
		t.Run(p.name, func(t *testing.T) {
			spy := newLogSpy()
			opts := Options{
				GOOS:     "linux",
				LookPath: fakePaths("bash"),
				Logger:   spy.logger(),
				Exec: func(_ context.Context, c Command) (Outcome, error) {
					if raw, err := os.ReadFile(c.Args[len(c.Args)-1]); err == nil &&
						strings.Contains(string(raw), secret) {
						sawBodyOnDisk = true
					}
					return Outcome{ExitCode: 0, Output: "done"}, nil
				},
			}
			if p.options != nil {
				p.options(&opts)
			}
			_, err := New(opts).runScript(context.Background(), p.params)

			assertAbsent(t, spy.String(), secret, "the log")
			if err != nil {
				assertAbsent(t, err.Error(), secret, "the error/refusal reason")
			}
		})
	}
	if !sawBodyOnDisk {
		t.Fatal("the secret never reached the script file on any probe, so every absence " +
			"assertion above passed for the wrong reason. This test is vacuous")
	}
}

type startFailed struct{}

func (startFailed) Error() string { return "bash could not be started" }

var errStartFailed = startFailed{}

// ---------------------------------------------------------------------------
// Refusal classification
// ---------------------------------------------------------------------------

func TestAnInterpreterThisDeviceDoesNotHaveIsRefusedAndNotFailed(t *testing.T) {
	// `refused` and `failed` are different answers. An interpreter that is not
	// installed is a durable property of the device: retrying it forever is
	// wrong, and the control plane should be told this agent will not do it.
	//
	// FALSIFYING OBSERVATION: a plain error, which the runner reports as
	// `failed` and the control plane retries.
	spy := &execSpy{}
	svc := New(Options{
		GOOS: "linux", LookPath: nothingInstalled(), Logger: quiet(), Exec: spy.exec,
	})
	_, err := svc.runScript(context.Background(), scriptParamsJSON(t, "echo x", nil))
	reason := refusalReason(t, err)
	if !strings.Contains(reason, "nothing was executed") {
		t.Errorf("refusal reason %q does not say that nothing ran; that is the fact an operator "+
			"needs", reason)
	}
	spy.ranNothing(t, "no interpreter is installed")
}

func TestAProcessThatCouldNotBeStartedIsFailedAndNotRefused(t *testing.T) {
	// The other side of the same distinction. The interpreter resolved and
	// then would not start: that is transient, and a refusal would ask to be
	// taken out of rotation for it.
	svc := New(Options{
		GOOS: "linux", LookPath: fakePaths("bash"), Logger: quiet(),
		Exec: func(context.Context, Command) (Outcome, error) { return Outcome{}, errStartFailed },
	})
	_, err := svc.runScript(context.Background(), scriptParamsJSON(t, "echo x", nil))
	assertNotRefusal(t, err)
}

func TestBashOnWindowsIsRefusedRatherThanHuntedFor(t *testing.T) {
	// Quietly finding SOME bash-like thing on a Windows endpoint and running an
	// operator's script under it as SYSTEM is worse than saying no.
	spy := &execSpy{}
	svc := New(Options{
		// Every candidate is "installed", so a hunt would succeed if there were
		// one. The refusal must come from the plan, not from an absent binary.
		GOOS:     "windows",
		LookPath: fakePaths("bash", "pwsh.exe", "powershell.exe", "python3", "python"),
		Logger:   quiet(), Exec: spy.exec,
	})
	_, err := svc.runScript(context.Background(), scriptParamsJSON(t, "echo x", nil))
	refusalReason(t, err)
	spy.ranNothing(t, "a bash body was sent to a Windows device")
}

func TestAMalformedParamsDocumentIsRefusedBeforeAnythingIsWrittenToDisk(t *testing.T) {
	root := t.TempDir()
	spy := &execSpy{}
	svc := New(Options{
		GOOS: "linux", LookPath: fakePaths("bash"), Logger: quiet(), Exec: spy.exec,
		ScratchRoot: root,
	})
	for _, tc := range []struct {
		name   string
		params json.RawMessage
	}{
		{"not an object", json.RawMessage(`"a string"`)},
		{"empty body", scriptParamsJSON(t, "", nil)},
		{"absent script_id", scriptParamsJSON(t, "echo x", map[string]any{"script_id": nil})},
		{"script_id that is not an identifier",
			scriptParamsJSON(t, "echo x", map[string]any{"script_id": "a b; rm -rf /"})},
		{"timeout above the ceiling",
			scriptParamsJSON(t, "echo x", map[string]any{"timeout_s": maxTimeoutS + 1})},
		{"negative version",
			scriptParamsJSON(t, "echo x", map[string]any{"script_version": -1})},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := svc.runScript(context.Background(), tc.params)
			refusalReason(t, err)
		})
	}
	spy.ranNothing(t, "every params document above is malformed")
	entries, err := os.ReadDir(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("a rejected params document still created %d scratch entries; validation must "+
			"happen before anything touches the disk", len(entries))
	}
}

// ---------------------------------------------------------------------------
// Exit codes, timeouts, output
// ---------------------------------------------------------------------------

func TestOnlyExitCodeZeroIsSucceeded(t *testing.T) {
	// Treating a non-zero exit as success is how a failed remediation reports
	// green across a fleet.
	//
	// FALSIFYING OBSERVATION: any non-zero code producing `succeeded`. 3010
	// and 1641 are included deliberately: they ARE successes for an MSI, and
	// this asserts that app.deploy's leniency did not leak into script.run,
	// where a script's exit code means whatever its author decided.
	for _, code := range []int{1, 2, 255, -1, 3010, 1641} {
		svc := New(Options{
			GOOS: "linux", LookPath: fakePaths("bash"), Logger: quiet(),
			Exec: func(context.Context, Command) (Outcome, error) {
				return Outcome{ExitCode: code}, nil
			},
		})
		res, err := svc.runScript(context.Background(), scriptParamsJSON(t, "x", nil))
		if err != nil {
			t.Fatal(err)
		}
		if res.Outcome.State != stateFailed {
			t.Errorf("exit code %d produced state %q; script.run has no notion of a partial "+
				"success", code, res.Outcome.State)
		}
		if res.Outcome.ExitCode == nil || *res.Outcome.ExitCode != code {
			t.Errorf("exit code %d was not reported back", code)
		}
	}
}

func TestScriptRunEmitsOnlyItsOwnTwoStates(t *testing.T) {
	// app/modules/deploy/scripts.py::_SCRIPT_OUTCOME_STATES is {succeeded,
	// failed}. Anything else is COERCED to `failed` server-side with the
	// original recorded as an anomaly — so emitting `installed` here would make
	// a successful script read as a failed one.
	for _, out := range []Outcome{
		{ExitCode: 0},
		{ExitCode: 1},
		{ExitCode: 0, TimedOut: true},
		{ExitCode: 0, Output: strings.Repeat("x", 100)},
	} {
		got := scriptOutcome("id@v1", out)
		if got.State != stateSucceeded && got.State != stateFailed {
			t.Fatalf("scriptOutcome produced state %q; script.run's vocabulary is exactly "+
				"{succeeded, failed}", got.State)
		}
	}
}

func TestAKilledProcessIsFailedEvenWhenItExitedZero(t *testing.T) {
	// A process killed at its deadline frequently reports a zero-ish exit code
	// on Windows. Reporting that as success would mean a script that was cut
	// off halfway is indistinguishable from one that finished.
	got := scriptOutcome("id@v1", Outcome{ExitCode: 0, TimedOut: true, Output: "partial"})
	if got.State != stateFailed {
		t.Fatalf("a killed process reported state %q; it must be %q", got.State, stateFailed)
	}
	if !strings.Contains(got.Log, "deadline passed") {
		t.Fatalf("the log does not say the process was killed: %q", got.Log)
	}
}

func TestOutputIsBoundedAndTheTailIsKept(t *testing.T) {
	// An installer or a script writing a gigabyte to stdout must not become a
	// gigabyte in a job result. The TAIL is kept because that is where the
	// error is.
	//
	// FALSIFYING OBSERVATION: a log longer than the cap, or one that kept the
	// head and lost the error at the end.
	const marker = "THE-ERROR-IS-HERE"
	huge := strings.Repeat("banner line that nobody needs\n", 200_000) + marker
	got := wireLog(Outcome{Output: huge})
	if len(got) > maxLogChars+64 {
		t.Fatalf("wireLog returned %d characters for a %d-character stream; the cap is %d",
			len(got), len(huge), maxLogChars)
	}
	if !strings.Contains(got, marker) {
		t.Fatal("the tail was discarded and the head kept. An installer that fails says why on " +
			"its last few lines")
	}
	if !strings.Contains(got, "truncated") {
		t.Fatal("a truncated log is not marked as truncated; an operator would read it as " +
			"complete")
	}
}

func TestOutputIsStrippedToPrintableASCIIBeforeItReachesAConsole(t *testing.T) {
	// The bytes come from a program the operator chose but did not write.
	// Bidi overrides and control characters in a console where one job's
	// record is read next to another's are a way to make one record read as
	// another — the same argument internal/jobs' sanitize makes for refusal
	// reasons.
	// The RLO is written as \u202e rather than embedded literally: an invisible
	// bidi override in source is exactly the trick this test exists to defeat,
	// and it should not be readable only in a hex editor (ST1018). Same bytes.
	got := wireLog(Outcome{Output: "before\u202eafter\x00\x07\r\nend\ttabbed"})
	for _, bad := range []string{"\u202e", "\x00", "\x07", "\r"} {
		if strings.Contains(got, bad) {
			t.Fatalf("wireLog kept %q", bad)
		}
	}
	if !strings.Contains(got, "end\ttabbed") {
		t.Fatalf("wireLog mangled ordinary text: %q", got)
	}
	if !strings.Contains(got, "\n") {
		t.Fatal("wireLog dropped newlines; a multi-line installer log would become one line")
	}
}

// ---------------------------------------------------------------------------
// The file name, and why the extension matters
// ---------------------------------------------------------------------------

func TestAPowerShellBodyIsWrittenAsDotPS1(t *testing.T) {
	// PowerShell's -File refuses a file without a .ps1 extension. That is a
	// small, free check that this agent is running what it thinks it is.
	if got := scriptFileName("powershell"); filepath.Ext(got) != ".ps1" {
		t.Fatalf("a powershell body is written as %q; -File requires .ps1", got)
	}
	if got := scriptFileName("bash"); filepath.Ext(got) != ".sh" {
		t.Fatalf("a bash body is written as %q", got)
	}
	if got := scriptFileName("python"); filepath.Ext(got) != ".py" {
		t.Fatalf("a python body is written as %q", got)
	}
}

// ---------------------------------------------------------------------------
// The identifier
// ---------------------------------------------------------------------------

func TestTheIdentifierNamesTheScriptAndItsVersionAndIsBounded(t *testing.T) {
	svc := New(Options{
		GOOS: "linux", LookPath: fakePaths("bash"), Logger: quiet(),
		Exec: func(context.Context, Command) (Outcome, error) { return Outcome{}, nil },
	})
	res, err := svc.runScript(context.Background(), scriptParamsJSON(t, "x", nil))
	if err != nil {
		t.Fatal(err)
	}
	want := "11111111-2222-3333-4444-555555555555@v7"
	if res.Outcome.Identifier != want {
		t.Fatalf("identifier = %q, want %q", res.Outcome.Identifier, want)
	}
	if got := clampIdentifier(strings.Repeat("a", maxIdentifier*3)); len(got) != maxIdentifier {
		t.Fatalf("clampIdentifier produced %d characters; the bound is %d", len(got), maxIdentifier)
	}
}

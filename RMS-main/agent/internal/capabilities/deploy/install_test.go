package deploy

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"strings"
	"testing"
)

// `app.deploy` behaviour that is not the hash check: refusal classification,
// argument validation, detection, the two wire-contract gaps, and the states
// the result may carry.

func wingetOptions(t *testing.T) (Options, *execSpy) {
	t.Helper()
	spy := &execSpy{}
	return Options{
		GOOS: "windows", LookPath: fakePaths("winget.exe", "choco.exe", "msiexec.exe"),
		Logger: quiet(), Exec: spy.exec, ScratchRoot: t.TempDir(),
		Fetch: func(context.Context, string) (io.ReadCloser, error) {
			return io.NopCloser(bytes.NewReader(nil)), nil
		},
	}, spy
}

func wingetParams(t *testing.T, extra map[string]any) json.RawMessage {
	t.Helper()
	doc := map[string]any{
		"installer_type": "winget",
		"detection_rule": map[string]any{"type": "package_installed", "name": "Mozilla.Firefox"},
	}
	for k, v := range extra {
		doc[k] = v
	}
	return deployParamsJSON(t, doc)
}

// ---------------------------------------------------------------------------
// Refused vs failed
// ---------------------------------------------------------------------------

func TestAnUnsupportedPackageManagerIsRefusedAndAFlakyNetworkIsFailed(t *testing.T) {
	// The task's own line, and the one the control plane acts on: `refused`
	// asks to be taken out of rotation, `failed` asks to be retried.
	//
	// FALSIFYING OBSERVATION: either answer given for the other case.
	t.Run("winget is not installed on this device", func(t *testing.T) {
		spy := &execSpy{}
		opts, _ := wingetOptions(t)
		opts.LookPath = nothingInstalled()
		opts.Exec = spy.exec
		_, err := New(opts).deploy(context.Background(), wingetParams(t, nil))
		reason := refusalReason(t, err)
		if !strings.Contains(reason, "winget") {
			t.Errorf("the refusal %q does not name the missing manager", reason)
		}
		spy.ranNothing(t, "winget is not installed")
	})

	t.Run("the artifact download failed", func(t *testing.T) {
		// A transport failure is the device being fine and the network not.
		// Refusing would ask to be taken out of rotation for a bad five
		// minutes.
		a := newArtifact("installer bytes")
		opts, spy, _ := msiJob(t, a, a.sha)
		opts.Fetch = func(context.Context, string) (io.ReadCloser, error) {
			return nil, errNetwork
		}
		_, err := New(opts).deploy(context.Background(), msiParams(t, a.sha))
		assertNotRefusal(t, err)
		spy.ranNothing(t, "the download failed")
	})

	t.Run("the download was cut off midway", func(t *testing.T) {
		a := newArtifact("installer bytes that never finish arriving")
		opts, spy, _ := msiJob(t, a, a.sha)
		opts.Fetch = func(context.Context, string) (io.ReadCloser, error) {
			return io.NopCloser(&failingReader{after: 5}), nil
		}
		_, err := New(opts).deploy(context.Background(), msiParams(t, a.sha))
		assertNotRefusal(t, err)
		spy.ranNothing(t, "the download was cut off")
	})
}

type networkErr struct{}

func (networkErr) Error() string { return "connection reset" }

var errNetwork = networkErr{}

// failingReader yields `after` bytes and then fails, which is what a dropped
// connection looks like to io.Copy.
type failingReader struct{ after int }

func (r *failingReader) Read(p []byte) (int, error) {
	if r.after <= 0 {
		return 0, errNetwork
	}
	n := len(p)
	if n > r.after {
		n = r.after
	}
	for i := 0; i < n; i++ {
		p[i] = 'x'
	}
	r.after -= n
	return n, nil
}

func TestAnS3ArtifactRefIsRefusedWithTheMissingRouteNamed(t *testing.T) {
	// A REAL GAP IN THE WIRE CONTRACT, made loud. `artifact_ref` is an
	// `s3://bucket/key` URI (ObjectStore.put_package_artifact) and there is no
	// agent-facing route that resolves one. The agent holds no object-store
	// credentials and must not (invariant #3).
	//
	// Refusal and not failure: retrying does not grow a route.
	//
	// FALSIFYING OBSERVATION: a download attempted, a process spawned, or a
	// plain failure that a dashboard would show as a transient error forever.
	a := newArtifact("installer bytes")
	spy := &execSpy{}
	opts, _, _ := msiJob(t, a, a.sha)
	opts.Exec = spy.exec
	opts.Fetch = nil // the PRODUCTION fetcher, which is the subject here
	params := deployParamsJSON(t, map[string]any{
		"artifact_ref":    "s3://it-suite/00000000-0000-0000-0000-000000000000/package-artifacts/x",
		"artifact_sha256": a.sha,
	})
	_, err := New(opts).deploy(context.Background(), params)
	reason := refusalReason(t, err)
	for _, want := range []string{"s3://", "https://", "credentials"} {
		if !strings.Contains(reason, want) {
			t.Errorf("the refusal %q does not mention %q; a contract gap that is not named is a "+
				"permanent mystery in a dashboard", reason, want)
		}
	}
	spy.ranNothing(t, "the artifact ref cannot be resolved")
}

func TestOnlyHTTPSArtifactRefsAreAccepted(t *testing.T) {
	// Not `file://` (which would turn artifact_ref into "execute this file
	// already on the box"), not `http://`, not a bare path.
	for _, ref := range []string{
		"file:///tmp/evil.msi",
		"http://packages.example/pkg.msi",
		"/tmp/evil.msi",
		`C:\Windows\Temp\evil.msi`,
		"ftp://packages.example/pkg.msi",
	} {
		_, err := defaultFetcher(context.Background(), ref)
		if err == nil {
			t.Errorf("defaultFetcher accepted %q", ref)
			continue
		}
		if !isRefusal(err) {
			t.Errorf("defaultFetcher failed rather than refusing for %q: %v", ref, err)
		}
		assertAbsent(t, err.Error(), ref, "the refusal reason")
	}
}

// ---------------------------------------------------------------------------
// The package identifier for winget/choco: two sources, three states
// ---------------------------------------------------------------------------
//
// `package_identifier` was added to AppDeployParams after this capability
// shipped (server-side migration 0052 + `artifacts.manager_package_identifier`;
// the schema guard in schema_test.go is what caught its arrival). It is
// ADDITIVE AND OPTIONAL, so the agent has to handle three states, and the
// tests below take them one at a time.
//
// THE FIXTURE RULE FOR ALL OF THEM: `package_identifier` and
// `detection_rule.name` CARRY DIFFERENT VALUES. A fixture where the two agree
// cannot tell an implementation that reads the new member from one that
// ignores it — both spawn the identical argv and both pass. Every assertion
// below therefore names the string it expects AND asserts the absence of the
// other one.

const (
	// explicitID is what `package_identifier` carries.
	explicitID = "Mozilla.Firefox"
	// ruleID is what `detection_rule.name` carries. DELIBERATELY DIFFERENT
	// from explicitID — see the fixture rule above.
	ruleID = "Some.OtherPackage"
)

// managerArgv runs one winget deployment and returns the argv it produced.
func managerArgv(t *testing.T, doc map[string]any) (Command, *deployResult) {
	t.Helper()
	opts, spy := wingetOptions(t)
	full := map[string]any{"installer_type": "winget"}
	for k, v := range doc {
		full[k] = v
	}
	res, err := New(opts).deploy(context.Background(), deployParamsJSON(t, full))
	if err != nil {
		t.Fatalf("the deployment failed: %v", err)
	}
	runs := spy.calls()
	if len(runs) != 1 {
		t.Fatalf("expected exactly one spawned process, got %d: %+v", len(runs), runs)
	}
	return runs[0], res
}

// assertArgvNames asserts that `want` is the identifier on the command line and
// that `absent` is nowhere near it.
func assertArgvNames(t *testing.T, cmd Command, want, absent string) {
	t.Helper()
	found := ""
	for i, a := range cmd.Args {
		if a == "--id" && i+1 < len(cmd.Args) {
			found = cmd.Args[i+1]
		}
	}
	if found != want {
		t.Errorf("winget was given --id %q, want %q; argv was %v", found, want, cmd.Args)
	}
	for _, a := range cmd.Args {
		if a == absent {
			t.Errorf("argv %v carries %q, which is the source that should NOT have been used",
				cmd.Args, absent)
		}
	}
}

// STATE 1: the field is present. It is what reaches the manager.
func TestFieldPresentIsTheIdentifierThatReachesTheManager(t *testing.T) {
	// FALSIFYING OBSERVATION: `winget --id Some.OtherPackage`, i.e. the agent
	// went on reading detection_rule.name and ignored the explicit member the
	// control plane now sends. That is the failure this whole change exists to
	// prevent, and it is invisible unless the two sources differ.
	cmd, res := managerArgv(t, map[string]any{
		"package_identifier": explicitID,
		"detection_rule":     map[string]any{"type": "package_installed", "name": ruleID},
	})
	assertArgvNames(t, cmd, explicitID, ruleID)

	// The identifier the operator sees in the result is the same one.
	if got := res.Outcomes[0].Identifier; got != explicitID {
		t.Errorf("the outcome identifier is %q, want %q", got, explicitID)
	}
}

// STATE 2: the field is absent. detection_rule.name still works.
func TestFieldAbsentFallsBackToDetectionRuleName(t *testing.T) {
	// THIS IS THE MIXED-VERSION GUARD, and it is the direction CLAUDE.md calls
	// an outage: "a field that a new control plane requires and an old agent
	// does not send". Here it is the mirror — an OLD CONTROL PLANE that does
	// not send the field talking to THIS agent. If this agent came to require
	// `package_identifier`, every deployment from a control plane that has not
	// yet shipped migration 0052 would refuse, and the test suite would not
	// show it because the suite only ever runs one version of each side.
	//
	// FALSIFYING OBSERVATION: a refusal, or an empty --id, when the member is
	// simply not in the document.
	cmd, res := managerArgv(t, map[string]any{
		"detection_rule": map[string]any{"type": "package_installed", "name": ruleID},
	})
	// `absent` here is explicitID, which is not in the document at all — the
	// assertion is that the agent did not conjure it from somewhere.
	assertArgvNames(t, cmd, ruleID, explicitID)
	if got := res.Outcomes[0].Identifier; got != ruleID {
		t.Errorf("the outcome identifier is %q, want %q", got, ruleID)
	}

	// An explicit JSON `null` is the same state as an absent member, and it is
	// the one the server actually sends for a catalog row with no explicit
	// value (Pydantic serialises `None`, it does not omit the key).
	cmdNull, _ := managerArgv(t, map[string]any{
		"package_identifier": nil,
		"detection_rule":     map[string]any{"type": "package_installed", "name": ruleID},
	})
	assertArgvNames(t, cmdNull, ruleID, explicitID)
}

// STATE 3: both absent. Refused, naming both sources.
func TestBothAbsentIsRefusedAndNamesBothSources(t *testing.T) {
	// The agent does not invent an identifier and does not parse one out of
	// install_args. The refusal has to name where it looked, or the operator
	// cannot fix it.
	//
	// FALSIFYING OBSERVATION: a winget process spawned at all, or a refusal
	// that names neither source.
	for _, doc := range []map[string]any{
		{},
		{"detection_rule": map[string]any{}},
		{"package_identifier": nil},
		{"package_identifier": "", "detection_rule": map[string]any{}},
		{"package_identifier": "   ", "detection_rule": map[string]any{"name": "  "}},
		{"detection_rule": map[string]any{"type": "package_installed", "name": 42}},
	} {
		opts, spy := wingetOptions(t)
		full := map[string]any{"installer_type": "winget"}
		for k, v := range doc {
			full[k] = v
		}
		_, err := New(opts).deploy(context.Background(), deployParamsJSON(t, full))
		reason := refusalReason(t, err)
		for _, source := range []string{"package_identifier", "detection_rule.name"} {
			if !strings.Contains(reason, source) {
				t.Errorf("the refusal %q does not name %s, so an operator cannot tell where the "+
					"agent looked; document was %v", reason, source, full)
			}
		}
		spy.ranNothing(t, "the deployment carries no usable package identifier")
	}
}

// The two sources get the SAME flag guard. A new field that skipped it would be
// a hole opened by an additive change.
func TestAnExplicitIdentifierThatCouldBecomeAFlagIsRefusedToo(t *testing.T) {
	// The MUTATION relative to TestAPackageIdentifierThatCouldBecomeAFlagIsRefused
	// is the SOURCE, not the value: the same hostile strings arrive through
	// `package_identifier` instead of `detection_rule.name`. An implementation
	// that validated only the old source would pass that test and fail this
	// one.
	//
	// The detection rule carries a PERFECTLY GOOD identifier throughout, so a
	// refusal cannot be explained by "there was nothing usable" — the agent had
	// a valid fallback available and must still refuse rather than quietly
	// preferring it. Silently falling back on a hostile value would turn an
	// attack into a successful install of something else.
	for _, id := range []string{
		"--uninstall", "-h", "/force", "Some Package", "pkg;whoami", "pkg&calc",
		"pkg|calc", "pkg$(id)", "pkg`id`", "pkg\nid", "pkg%TEMP%", "pkg!x!", "pkg^x",
		strings.Repeat("a", maxPackageID+1),
	} {
		opts, spy := wingetOptions(t)
		params := deployParamsJSON(t, map[string]any{
			"installer_type":     "winget",
			"package_identifier": id,
			"detection_rule":     map[string]any{"type": "package_installed", "name": ruleID},
		})
		_, err := New(opts).deploy(context.Background(), params)
		if err == nil {
			t.Errorf("the explicit package identifier %q was accepted", id)
			continue
		}
		reason := refusalReason(t, err)
		assertAbsent(t, reason, id, "the refusal reason")
		spy.ranNothing(t, "the explicit package identifier "+id+" is not a plain identifier")
	}
}

func TestAnEmptyExplicitIdentifierFallsThroughToTheDetectionRule(t *testing.T) {
	// Present-but-blank is not the same as hostile: the two members are
	// alternative spellings of one fact, and a control plane that sent "" beside
	// a good rule meant the rule. Falling through is the useful reading and
	// costs nothing — an empty string could not have reached an argv anyway.
	//
	// FALSIFYING OBSERVATION: a refusal, or an empty --id on the command line.
	for _, blank := range []string{"", "   ", "\t"} {
		cmd, _ := managerArgv(t, map[string]any{
			"package_identifier": blank,
			"detection_rule":     map[string]any{"type": "package_installed", "name": ruleID},
		})
		assertArgvNames(t, cmd, ruleID, explicitID)
	}
}

func TestAnArtifactBackedJobIgnoresPackageIdentifierRatherThanRefusing(t *testing.T) {
	// The DELIBERATE ASYMMETRY with parseDeployParams, which refuses a
	// manager-sourced job carrying `artifact_ref`.
	//
	// An artifact_ref on a manager job is refused because tolerating it is the
	// direction that could one day reach an execution path with no hash check.
	// `package_identifier` selects no branch and is consumed by nothing on an
	// artifact-backed job, so refusing it would only mean that a future
	// server-side change which set it harmlessly took the fleet down. The
	// server sends None here today (`build_deploy_params`), and this test is
	// what says the agent survives the day it does not.
	//
	// FALSIFYING OBSERVATION: a refusal, or the identifier appearing in the
	// argv or the outcome of an artifact-backed deployment.
	a := newArtifact("installer")
	opts, spy, _ := msiJob(t, a, a.sha)
	params := deployParamsJSON(t, map[string]any{
		"installer_type":     "msi",
		"artifact_ref":       "https://packages.example/pkg.msi",
		"artifact_sha256":    a.sha,
		"package_identifier": explicitID,
	})
	res, err := New(opts).deploy(context.Background(), params)
	if err != nil {
		t.Fatalf("an artifact-backed job carrying an inert package_identifier was rejected: %v", err)
	}
	runs := spy.calls()
	if len(runs) != 1 {
		t.Fatalf("expected one spawned process, got %d", len(runs))
	}
	for _, arg := range runs[0].Args {
		if strings.Contains(arg, explicitID) {
			t.Errorf("argv %v carries the package identifier; an artifact-backed deployment names "+
				"itself by installer type and action and consumes nothing job-supplied", runs[0].Args)
		}
	}
	if got := res.Outcomes[0].Identifier; got != "msi/install" {
		t.Errorf("the outcome identifier is %q, want %q", got, "msi/install")
	}
}

func TestAManagerDeploymentWithNoIdentifierIsRefusedWithTheGapNamed(t *testing.T) {
	// The original shape of state 3, kept because it enumerates the malformed
	// detection rules rather than the malformed identifiers, and because it is
	// the test that existed before `package_identifier` did: it asserts that
	// adding the new source did not stop the OLD one from being read.
	//
	// FALSIFYING OBSERVATION: a winget process spawned with an identifier this
	// agent made up.
	for _, rule := range []any{
		nil,
		map[string]any{},
		map[string]any{"type": "file_exists", "path": `C:\x`},
		map[string]any{"type": "package_installed"},
		map[string]any{"type": "package_installed", "name": ""},
		map[string]any{"type": "package_installed", "name": 42},
	} {
		opts, spy := wingetOptions(t)
		doc := map[string]any{"installer_type": "winget"}
		if rule != nil {
			doc["detection_rule"] = rule
		}
		_, err := New(opts).deploy(context.Background(), deployParamsJSON(t, doc))
		reason := refusalReason(t, err)
		if !strings.Contains(reason, "detection_rule.name") {
			t.Errorf("the refusal %q does not name where the identifier is read from", reason)
		}
		spy.ranNothing(t, "the deployment carries no package identifier")
	}
}

func TestAPackageIdentifierThatCouldBecomeAFlagIsRefused(t *testing.T) {
	// `winget install --id --uninstall` is not a package named `--uninstall`;
	// it is a flag. An identifier that can become a flag can change the verb,
	// and the character allowlist alone does not stop it because `-` is a
	// legitimate character INSIDE a package id.
	//
	// FALSIFYING OBSERVATION: any of these reaching an argv.
	for _, id := range []string{
		"--uninstall", "-h", "/force", "Some Package", "pkg;whoami", "pkg&calc",
		"pkg|calc", "pkg$(id)", "pkg`id`", "pkg\nid", "pkg%TEMP%", "pkg!x!", "pkg^x",
		strings.Repeat("a", maxPackageID+1),
	} {
		opts, spy := wingetOptions(t)
		params := deployParamsJSON(t, map[string]any{
			"installer_type": "winget",
			"detection_rule": map[string]any{"type": "package_installed", "name": id},
		})
		_, err := New(opts).deploy(context.Background(), params)
		if err == nil {
			t.Errorf("the package identifier %q was accepted", id)
			continue
		}
		refusalReason(t, err)
		spy.ranNothing(t, "the package identifier "+id+" is not a plain identifier")
	}

	// And the counterpart: real package ids must still work, or the rule above
	// is just "refuse everything".
	for _, id := range []string{
		"Mozilla.Firefox", "googlechrome", "Microsoft.VisualStudioCode",
		"7zip.install", "notepadplusplus-nonadmin", "Docker.DockerDesktop",
	} {
		if !validPackageID(id) {
			t.Errorf("the real package id %q was rejected; the allowlist is too narrow to be "+
				"usable, and an unusable rule gets widened by whoever hits it next", id)
		}
	}
}

// ---------------------------------------------------------------------------
// install_args
// ---------------------------------------------------------------------------

func TestInstallArgsBecomeArgvElementsAndNeverACommandLine(t *testing.T) {
	a := newArtifact("installer")
	opts, spy, _ := msiJob(t, a, a.sha)
	params := deployParamsJSON(t, map[string]any{
		"artifact_ref":    "https://packages.example/pkg.msi",
		"artifact_sha256": a.sha,
		"install_args":    "REBOOT=ReallySuppress ALLUSERS=1 /log C:\\ProgramData\\install.log",
	})
	if _, err := New(opts).deploy(context.Background(), params); err != nil {
		t.Fatal(err)
	}
	runs := spy.calls()
	if len(runs) != 1 {
		t.Fatalf("%d spawns", len(runs))
	}
	tail := runs[0].Args[len(runs[0].Args)-4:]
	want := []string{"REBOOT=ReallySuppress", "ALLUSERS=1", "/log", `C:\ProgramData\install.log`}
	if strings.Join(tail, "\x00") != strings.Join(want, "\x00") {
		t.Fatalf("the tail of the argv is %v, want %v. Each token must be its OWN argv element",
			tail, want)
	}
}

func TestInstallArgsCarryingAnythingOutsideTheAllowlistAreRefused(t *testing.T) {
	// On POSIX this is belt-and-braces; on Windows it is not, because msiexec,
	// winget and choco each re-parse their own arguments and cmd-style escapes
	// survive several of those layers.
	//
	// FALSIFYING OBSERVATION: any of these reaching an argv, or an attempt to
	// ESCAPE rather than refuse — a repaired argument is an argument nobody
	// reviewed.
	a := newArtifact("installer")
	for _, args := range []string{
		`/log "C:\Program Files\x.log"`, // the documented limitation
		"/qn & calc.exe",
		"/qn | calc.exe",
		"/qn ; calc.exe",
		"/qn $(calc.exe)",
		"/qn `calc.exe`",
		"/qn %TEMP%",
		"/qn ^calc",
		"/qn !DELAYED!",
		"/qn <input.txt",
		"/qn >output.txt",
		"/qn 'quoted'",
		"/qn *.msi",
		strings.Repeat("/qn ", maxArgTokens+1),
		"/log " + strings.Repeat("a", maxArgLen+1),
	} {
		opts, spy, _ := msiJob(t, a, a.sha)
		params := deployParamsJSON(t, map[string]any{
			"artifact_ref":    "https://packages.example/pkg.msi",
			"artifact_sha256": a.sha,
			"install_args":    args,
		})
		_, err := New(opts).deploy(context.Background(), params)
		if err == nil {
			t.Errorf("install_args %q was accepted", args)
			continue
		}
		refusalReason(t, err)
		spy.ranNothing(t, "install_args carries a character this agent will not pass on")
	}
}

func TestARefusalAboutInstallArgsNamesThePositionAndNotTheContent(t *testing.T) {
	// The refusal reason lands in the server's hash-chained audit log, and
	// install_args is exactly the kind of field an operator puts a credential
	// in. The operator can find argument 3 in the console they typed it into.
	//
	// FALSIFYING OBSERVATION: the secret in the refusal reason.
	const secret = "P@ssw0rd-DOMAIN-ADMIN-DO-NOT-LOG"
	spy := newLogSpy()
	a := newArtifact("installer")
	opts, _, _ := msiJob(t, a, a.sha)
	opts.Logger = spy.logger()
	params := deployParamsJSON(t, map[string]any{
		"artifact_ref":    "https://packages.example/pkg.msi",
		"artifact_sha256": a.sha,
		"install_args":    "/qn ADMINPASS='" + secret + "'",
	})
	_, err := New(opts).deploy(context.Background(), params)
	if err == nil {
		t.Fatal("the quoted argument was accepted; this test would be vacuous")
	}
	assertAbsent(t, err.Error(), secret, "the refusal reason")
	assertAbsent(t, spy.String(), secret, "the log")
	if !strings.Contains(err.Error(), "argument 2") {
		t.Errorf("the refusal %q does not name which argument was rejected; an operator has "+
			"nothing to act on", err)
	}
}

func TestInstallArgsThatAreAcceptedNeverAppearInTheLog(t *testing.T) {
	// The success path of the same rule. An accepted argument is still an
	// operator's secret.
	const secret = "SILENTPASS-abc123XYZ"
	spy := newLogSpy()
	a := newArtifact("installer")
	opts, _, _ := msiJob(t, a, a.sha)
	opts.Logger = spy.logger()
	params := deployParamsJSON(t, map[string]any{
		"artifact_ref":    "https://packages.example/pkg.msi",
		"artifact_sha256": a.sha,
		"install_args":    "ADMINPASS=" + secret,
	})
	if _, err := New(opts).deploy(context.Background(), params); err != nil {
		t.Fatalf("a well-formed argument was rejected: %v; this test would be vacuous", err)
	}
	assertAbsent(t, spy.String(), secret, "the log")
	if spy.String() == "" {
		t.Fatal("nothing was logged at all, so the absence above proves nothing")
	}
}

// ---------------------------------------------------------------------------
// Detection
// ---------------------------------------------------------------------------

func TestADeviceAlreadyInTheDesiredStateCostsNoDownloadAndNoProcess(t *testing.T) {
	// AD-06, re-checked agent-side. A device already there is
	// `not_applicable`, and the download does not happen either.
	//
	// FALSIFYING OBSERVATION: a fetch or a spawn for a device that already has
	// the package.
	a := newArtifact("installer")
	fetched := 0
	spy := &execSpy{}
	opts, _, _ := msiJob(t, a, a.sha)
	opts.Exec = spy.exec
	opts.Fetch = func(context.Context, string) (io.ReadCloser, error) {
		fetched++
		return io.NopCloser(bytes.NewReader(a.body)), nil
	}
	opts.Stat = func(path string) bool { return path == `C:\Program Files\Thing\thing.exe` }

	params := deployParamsJSON(t, map[string]any{
		"artifact_ref":    "https://packages.example/pkg.msi",
		"artifact_sha256": a.sha,
		"detection_rule": map[string]any{
			"type": "file_exists", "path": `C:\Program Files\Thing\thing.exe`,
		},
	})
	res, err := New(opts).deploy(context.Background(), params)
	if err != nil {
		t.Fatal(err)
	}
	if res.Outcomes[0].State != stateNotApplicable {
		t.Fatalf("state = %q, want %q", res.Outcomes[0].State, stateNotApplicable)
	}
	spy.ranNothing(t, "the detection rule already reports the desired state")
	if fetched != 0 {
		t.Fatalf("the artifact was downloaded %d time(s) for a device already in the desired "+
			"state", fetched)
	}
	if res.Outcomes[0].DetectedAfter == nil || !*res.Outcomes[0].DetectedAfter {
		t.Error("detected_after is not true for a device the rule already matches")
	}
}

func TestUninstallInvertsTheDetectionRule(t *testing.T) {
	// Mirrors app/modules/deploy/detect.py: for uninstall the desired state is
	// the rule NOT matching. Getting this backwards uninstalls things that are
	// already gone and skips the ones that are there.
	a := newArtifact("installer")
	for _, tc := range []struct {
		present bool
		want    string
	}{
		{present: false, want: stateNotApplicable}, // already gone
		{present: true, want: stateInstalled},      // there; uninstall it
	} {
		spy := &execSpy{}
		opts, _, _ := msiJob(t, a, a.sha)
		opts.Exec = spy.exec
		opts.Stat = func(string) bool { return tc.present }
		params := deployParamsJSON(t, map[string]any{
			"action":          "uninstall",
			"artifact_ref":    "https://packages.example/pkg.msi",
			"artifact_sha256": a.sha,
			"detection_rule":  map[string]any{"type": "file_exists", "path": `C:\x`},
		})
		res, err := New(opts).deploy(context.Background(), params)
		if err != nil {
			t.Fatal(err)
		}
		if res.Outcomes[0].State != tc.want {
			t.Errorf("present=%v produced state %q, want %q", tc.present, res.Outcomes[0].State,
				tc.want)
		}
	}
}

func TestARuleThisBuildCannotEvaluateReportsDetectedAfterAsNullRatherThanFalse(t *testing.T) {
	// The three-valued field, and why it matters: reporting `false` for a rule
	// this build cannot evaluate would show every successful install of a
	// registry-detected package as "installed but not detected" — an operator
	// chasing a non-existent problem on every device in the fleet.
	//
	// FALSIFYING OBSERVATION: a non-nil detected_after for any of these.
	for _, rule := range []map[string]any{
		{},
		{"type": "registry_value", "key": `HKLM\SOFTWARE\x`},
		{"type": "package_installed", "name": "Thing"},
		{"type": "command_exit_zero", "command": "whoami"},
		{"type": "file_exists"},             // no path
		{"type": "file_exists", "path": 42}, // wrong type
		{"type": 7, "path": `C:\x`},         // wrong discriminator type
		{"type": "something_new_server_side"},
	} {
		got := evaluate(rule, func(string) bool { return true })
		if got.Known {
			t.Errorf("evaluate(%v) claimed to know the answer", rule)
		}
		if detectedAfter(got) != nil {
			t.Errorf("detectedAfter(%v) is not nil", rule)
		}
		if desiredStateReached("install", got) {
			t.Errorf("desiredStateReached said an unevaluable rule (%v) is already satisfied; "+
				"that would silently skip a deployment an operator asked for", rule)
		}
		if desiredStateReached("uninstall", got) {
			t.Errorf("desiredStateReached said an unevaluable rule (%v) is already satisfied "+
				"for uninstall", rule)
		}
	}
	// And the one rule that IS implemented actually answers, or every
	// assertion above passes for the wrong reason.
	known := evaluate(map[string]any{"type": "file_exists", "path": "/x"},
		func(string) bool { return true })
	if !known.Known || !known.Matched {
		t.Fatal("file_exists is not evaluated either; this test is vacuous")
	}
}

func TestCommandExitZeroIsNotASecondExecutionPath(t *testing.T) {
	// A DELIBERATE NON-IMPLEMENTATION, asserted so it stays deliberate. A
	// `command_exit_zero` rule would put a program on an endpoint from an
	// `additionalProperties: true` free-form dict — unlike a script body,
	// which is a first-class field with an authoring flow, an RBAC gate and a
	// per-version audit trail.
	//
	// FALSIFYING OBSERVATION: a spawn attributable to a detection rule.
	a := newArtifact("installer")
	spy := &execSpy{}
	opts, _, _ := msiJob(t, a, a.sha)
	opts.Exec = spy.exec
	params := deployParamsJSON(t, map[string]any{
		"artifact_ref":    "https://packages.example/pkg.msi",
		"artifact_sha256": a.sha,
		"detection_rule": map[string]any{
			"type": "command_exit_zero", "command": "/usr/bin/whoami", "args": []string{"-a"},
		},
	})
	if _, err := New(opts).deploy(context.Background(), params); err != nil {
		t.Fatal(err)
	}
	runs := spy.calls()
	// Exactly one spawn: the installer. Not two.
	if len(runs) != 1 {
		t.Fatalf("%d processes were spawned; only the installer should be", len(runs))
	}
	if strings.Contains(runs[0].Path, "whoami") {
		t.Fatal("a detection rule's command was executed")
	}
}

// ---------------------------------------------------------------------------
// The result
// ---------------------------------------------------------------------------

func TestTheInstallerExitCodesThatAreSuccessesAreTheWindowsOnesAndNoOthers(t *testing.T) {
	// 3010 (ERROR_SUCCESS_REBOOT_REQUIRED) and 1641
	// (ERROR_SUCCESS_REBOOT_INITIATED) are MSI successes. "Zero means good"
	// reports a fleet-wide rollout of a package that needs a reboot as a
	// fleet-wide failure.
	//
	// And 1605 is NOT forgiven, even though it is tempting: it means the
	// product was not there, which is a not_applicable the detection rule
	// should have caught, and calling it `installed` hides a broken rule
	// forever.
	for code, want := range map[int]string{
		0: stateInstalled, 3010: stateInstalled, 1641: stateInstalled,
		1: stateFailed, 1603: stateFailed, 1605: stateFailed, 1618: stateFailed,
		-1: stateFailed,
	} {
		got := deployOutcome("pkg", Outcome{ExitCode: code}, unknownDetection)
		if got.State != want {
			t.Errorf("exit code %d produced state %q, want %q", code, got.State, want)
		}
	}
}

func TestAppDeployEmitsOnlyItsOwnThreeStates(t *testing.T) {
	// app/modules/deploy/dispatch.py::_DEPLOY_OUTCOME_STATES is {installed,
	// failed, not_applicable}. `succeeded` — script.run's word — would be
	// coerced to `failed` with the original recorded as an anomaly.
	for _, out := range []Outcome{
		{ExitCode: 0}, {ExitCode: 1}, {ExitCode: 3010}, {ExitCode: 0, TimedOut: true},
	} {
		got := deployOutcome("pkg", out, unknownDetection)
		switch got.State {
		case stateInstalled, stateFailed, stateNotApplicable:
		default:
			t.Fatalf("deployOutcome produced state %q, which is not in deploy's vocabulary",
				got.State)
		}
	}
}

func TestTheResultIsAListOfExactlyOneOutcome(t *testing.T) {
	// AppDeployResult.outcomes is a list and ingest_deploy_result has an
	// explicit branch for the EMPTY one that lands the row on `failed` with
	// "agent returned no outcomes". Emitting an empty list would be a
	// self-inflicted failure.
	a := newArtifact("installer")
	opts, _, _ := msiJob(t, a, a.sha)
	res, err := New(opts).deploy(context.Background(), msiParams(t, a.sha))
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Outcomes) != 1 {
		t.Fatalf("the result carries %d outcomes; a deploy job is one device and one package",
			len(res.Outcomes))
	}
	raw, err := json.Marshal(res)
	if err != nil {
		t.Fatal(err)
	}
	// The server is extra="forbid": every member must be one it knows, and
	// every one this package emits must be present rather than omitted.
	for _, member := range []string{
		`"outcomes"`, `"identifier"`, `"state"`, `"exit_code"`, `"log"`, `"log_ref"`,
		`"detected_after"`,
	} {
		if !strings.Contains(string(raw), member) {
			t.Errorf("the marshalled result is missing %s: %s", member, raw)
		}
	}
}

func TestTheCountersOnlyMoveOnTheHandlerAndOnlyOnSuccess(t *testing.T) {
	// A dormancy assertion outside this package observes these counters, so
	// they have to mean what they say.
	svc := New(Options{
		GOOS: "linux", LookPath: fakePaths("bash"), Logger: quiet(),
		Exec: func(context.Context, Command) (Outcome, error) { return Outcome{}, nil },
	})
	if svc.Scripts() != 0 || svc.Deploys() != 0 {
		t.Fatal("a fresh Service has already run something")
	}
	if _, err := svc.runScript(context.Background(), scriptParamsJSON(t, "x", nil)); err != nil {
		t.Fatal(err)
	}
	if svc.Scripts() != 0 {
		t.Fatal("the internal runScript moved the counter; only the Run handler should")
	}
	if _, err := svc.runScript(context.Background(), json.RawMessage(`"nonsense"`)); err == nil {
		t.Fatal("a nonsense params document was accepted")
	}
	if svc.Scripts() != 0 || svc.Deploys() != 0 {
		t.Fatal("a refused job moved a counter")
	}
}

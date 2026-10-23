package patch

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// The apt engine, against REAL CAPTURED OUTPUT.
//
// testdata/apt-get-s-upgrade-real.txt is `apt-get -s -o Debug::NoLocking=1
// upgrade` run verbatim on the Ubuntu 24.04 container this package was written
// in. It is not a reconstruction and not an invention: 166 `Inst` lines, 136 of
// them from a `-security` pocket, multi-origin lines, and a trailing `[]`
// group this parser's author did not know existed until the capture showed it.
// That last one is the whole argument for capturing rather than inventing --
// an invented fixture would have had the shape the parser already handled.

// ---------------------------------------------------------------------------
// A recording runner
// ---------------------------------------------------------------------------

type recordedCall struct {
	Env  []string
	Name string
	Args []string
}

func (c recordedCall) argv() string { return c.Name + " " + strings.Join(c.Args, " ") }

type fakeRunner struct {
	calls   []recordedCall
	respond func(c recordedCall) commandResult
}

func (f *fakeRunner) run(ctx context.Context, env []string, name string, args ...string) commandResult {
	c := recordedCall{Env: env, Name: name, Args: args}
	f.calls = append(f.calls, c)
	if f.respond == nil {
		return commandResult{}
	}
	return f.respond(c)
}

func (f *fakeRunner) argvs() []string {
	out := make([]string, 0, len(f.calls))
	for _, c := range f.calls {
		out = append(out, c.argv())
	}
	return out
}

func fixture(t *testing.T, name string) string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatalf("fixture %s: %v", name, err)
	}
	return string(raw)
}

// aptWith returns an apt engine whose only shell-out answer is the given
// simulation output, plus the recorder.
func aptWith(t *testing.T, simulation string) (*aptEngine, *fakeRunner) {
	t.Helper()
	f := &fakeRunner{respond: func(c recordedCall) commandResult {
		if len(c.Args) > 0 && c.Args[0] == "-s" {
			return commandResult{Stdout: simulation}
		}
		return commandResult{}
	}}
	return &aptEngine{run: f.run, statFile: func(string) error { return os.ErrNotExist }}, f
}

// ---------------------------------------------------------------------------
// Parsing the real capture
// ---------------------------------------------------------------------------

func TestTheRealAptCaptureParsesIntoTheEntriesItActuallyContains(t *testing.T) {
	// FALSIFYING OBSERVATION: a count or a field value that differs from what
	// the captured file says. Every number below was read off the capture with
	// grep, not chosen.
	entries := parseAptSimulation(fixture(t, "apt-get-s-upgrade-real.txt"))

	const wantInst = 166
	if len(entries) != wantInst {
		t.Fatalf("parsed %d entries from a capture containing %d `Inst` lines", len(entries), wantInst)
	}

	byID := map[string]entry{}
	for _, e := range entries {
		if e.Source != sourceAPT {
			t.Fatalf("entry %q carries source %q", e.Identifier, e.Source)
		}
		byID[e.Identifier] = e
	}

	// The simplest line in the capture:
	//   Inst coreutils [9.4-3ubuntu6.1] (9.4-3ubuntu6.2 Ubuntu:24.04/noble-updates [amd64])
	got, ok := byID["coreutils@9.4-3ubuntu6.2"]
	if !ok {
		t.Fatalf("the capture's coreutils line did not become identifier "+
			"coreutils@9.4-3ubuntu6.2; got %d entries with ids like %v", len(entries), someKeys(byID, 5))
	}
	if got.Title != "coreutils 9.4-3ubuntu6.2" {
		t.Errorf("coreutils title = %q, want %q", got.Title, "coreutils 9.4-3ubuntu6.2")
	}
	if got.Severity != "unknown" {
		t.Errorf("coreutils came from noble-updates, not a security pocket, and got severity %q; "+
			"apt publishes no per-package severity and claiming one would be a fabrication",
			got.Severity)
	}
	if got.KB != nil || got.CVE != nil || got.SupersededBy != nil {
		t.Errorf("apt has no KB, CVE or supersede information and this entry claims some: %+v", got)
	}

	// The line whose SHAPE the capture taught: two origins and a trailing `[]`.
	//   Inst libc6-dbg [2.39-0ubuntu8.7] (2.39-0ubuntu8.8 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64]) []
	dbg, ok := byID["libc6-dbg@2.39-0ubuntu8.8"]
	if !ok {
		t.Fatalf("the capture's multi-origin libc6-dbg line did not parse; the trailing `[]` group " +
			"or the comma-separated origins broke it")
	}
	if dbg.Severity != "important" {
		t.Errorf("libc6-dbg comes from Ubuntu:24.04/noble-security and got severity %q, want "+
			"important; PD-02's auto-approve rule fires on critical|important, so this is the "+
			"difference between a security update auto-approving and sitting unapproved",
			dbg.Severity)
	}
}

func TestTheSecurityPocketCountMatchesTheCapture(t *testing.T) {
	// 136 of the capture's 166 `Inst` lines name a `-security` pocket
	// (`grep -c` on the file). A severity rule that matched everything, or
	// nothing, would still produce 166 entries -- which is exactly the
	// fixture-blind failure this project keeps hitting: the guarded and
	// unguarded paths agreeing on the value you happened to assert.
	//
	// FALSIFYING OBSERVATION: an important-count other than 136.
	entries := parseAptSimulation(fixture(t, "apt-get-s-upgrade-real.txt"))
	important := 0
	for _, e := range entries {
		if e.Severity == "important" {
			important++
		}
	}
	const wantImportant = 136
	if important != wantImportant {
		t.Fatalf("%d of %d entries are `important`; the capture has %d Inst lines naming a "+
			"-security pocket", important, len(entries), wantImportant)
	}
	if important == len(entries) {
		t.Fatal("every entry is important, so aptSeverity's two branches are indistinguishable " +
			"on this fixture and the test proves nothing")
	}
}

func TestAptSeverityDistinguishesTheArchiveAndNothingElse(t *testing.T) {
	for _, tc := range []struct {
		origins string
		want    string
	}{
		{"Ubuntu:24.04/noble-updates", "unknown"},
		{"Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security", "important"},
		{"Debian-Security:12/stable", "important"},
		{"Docker CE:noble", "unknown"},
		{"", "unknown"},
	} {
		if got := aptSeverity(tc.origins); got != tc.want {
			t.Errorf("aptSeverity(%q) = %q, want %q", tc.origins, got, tc.want)
		}
	}
}

func TestAnInstLineWithNoInstalledVersionGroupStillParses(t *testing.T) {
	// `upgrade` never installs a NEW package, so this shape does not appear in
	// the capture -- but `dist-upgrade` produces it and a future change to the
	// verb would otherwise silently drop every such line. Constructed, and
	// labelled as such.
	entries := parseAptSimulation("Inst newpkg (1.2-3 Ubuntu:24.04/noble-security [amd64])\n")
	if len(entries) != 1 {
		t.Fatalf("parsed %d entries, want 1", len(entries))
	}
	if entries[0].Identifier != "newpkg@1.2-3" || entries[0].Severity != "important" {
		t.Fatalf("got %+v", entries[0])
	}
}

func TestNonInstLinesContributeNothing(t *testing.T) {
	// apt's prose is localised and its wording changes between releases. A
	// parser that read any of it would break on a machine with LANG set.
	noise := strings.Join([]string{
		"Reading package lists...",
		"Building dependency tree...",
		"The following packages will be upgraded:",
		"  coreutils dpkg",
		"2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.",
		"Conf coreutils (9.4-3ubuntu6.2 Ubuntu:24.04/noble-updates [amd64])",
		"Remove oldpkg [1.0]",
	}, "\n")
	if got := parseAptSimulation(noise); len(got) != 0 {
		t.Fatalf("non-Inst lines produced %d entries: %+v", len(got), got)
	}
}

// ---------------------------------------------------------------------------
// The commands that run, and the ones that do not
// ---------------------------------------------------------------------------

func TestScanRefreshesTheIndexThenSimulatesAndNeverUsesAShell(t *testing.T) {
	e, f := aptWith(t, "Inst coreutils [1] (2 Ubuntu:24.04/noble-updates [amd64])\n")
	if _, err := e.Available(context.Background()); err != nil {
		t.Fatalf("Available: %v", err)
	}

	want := []string{
		"apt-get update -qq",
		"apt-get -s -o Debug::NoLocking=1 upgrade",
	}
	got := f.argvs()
	if len(got) != len(want) {
		t.Fatalf("ran %v, want exactly %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("call %d was %q, want %q", i, got[i], want[i])
		}
	}
	for _, c := range f.calls {
		assertNoShell(t, c)
	}
}

func TestAFailedIndexRefreshDoesNotFailTheScan(t *testing.T) {
	// A machine with no network right now still has a package index, and
	// reporting what that index knows beats reporting nothing. This is the
	// branch that would rot silently: `apt-get update` fails on a laptop in a
	// hotel every day.
	//
	// FALSIFYING OBSERVATION: an error, or an empty entry list, when only the
	// refresh failed.
	f := &fakeRunner{respond: func(c recordedCall) commandResult {
		if c.Args[0] == "update" {
			return commandResult{ExitCode: 100, Stderr: "Could not resolve host",
				Err: errors.New("exit status 100")}
		}
		return commandResult{Stdout: "Inst coreutils [1] (2 Ubuntu:24.04/noble-security [amd64])\n"}
	}}
	e := &aptEngine{run: f.run, statFile: func(string) error { return os.ErrNotExist }}

	got, err := e.Available(context.Background())
	if err != nil {
		t.Fatalf("a failed index refresh failed the whole scan: %v", err)
	}
	if len(got) != 1 || got[0].Severity != "important" {
		t.Fatalf("got %+v", got)
	}
}

func TestAFailedSimulationIsAPlainErrorAndNotARefusal(t *testing.T) {
	// jobs/verified.go: `refused` means "this agent will not do this" and takes
	// the device out of rotation for modules that read it. A held dpkg lock is
	// "try again", not "never".
	//
	// FALSIFYING OBSERVATION: a *jobs.Refusal coming back from a locked apt.
	f := &fakeRunner{respond: func(c recordedCall) commandResult {
		if c.Args[0] == "update" {
			return commandResult{}
		}
		return commandResult{
			ExitCode: 100,
			Stderr:   "E: Could not get lock /var/lib/dpkg/lock-frontend\n",
			Err:      errors.New("exit status 100"),
		}
	}}
	e := &aptEngine{run: f.run, statFile: func(string) error { return os.ErrNotExist }}

	_, err := e.Available(context.Background())
	if err == nil {
		t.Fatal("a failed simulation returned no error")
	}
	var refusal *jobs.Refusal
	if errors.As(err, &refusal) {
		t.Fatalf("a held dpkg lock was REFUSED (%q). A refusal asks the control plane to stop "+
			"dispatching to this device; a lock clears in minutes", refusal.Reason)
	}
	if !strings.Contains(err.Error(), "lock") {
		t.Errorf("the error does not carry apt's own message, which is the only actionable part "+
			"an operator gets: %v", err)
	}
}

func TestInstallPinsTheExactVersionAndSetsTheNoninteractiveFrontend(t *testing.T) {
	// FALSIFYING OBSERVATION: an argv that installs the CANDIDATE rather than
	// the approved version, or an install without DEBIAN_FRONTEND. The first
	// would install something the control plane never approved; the second
	// hangs on a debconf prompt until the job's deadline.
	e, f := aptWith(t, "")
	err := e.Install(context.Background(), entry{
		Source: sourceAPT, Identifier: "coreutils@9.4-3ubuntu6.2",
	})
	if err != nil {
		t.Fatalf("Install: %v", err)
	}
	if len(f.calls) != 1 {
		t.Fatalf("ran %v, want exactly one command", f.argvs())
	}
	c := f.calls[0]
	want := "apt-get -y --only-upgrade -o Dpkg::Options::=--force-confold " +
		"-o Dpkg::Options::=--force-confdef install coreutils=9.4-3ubuntu6.2"
	if c.argv() != want {
		t.Fatalf("argv was\n  %s\nwant\n  %s", c.argv(), want)
	}
	if !containsString(c.Env, "DEBIAN_FRONTEND=noninteractive") {
		t.Errorf("the install env is %v and does not set DEBIAN_FRONTEND; debconf would open a "+
			"dialog on a machine with no terminal and sit there until the job expires", c.Env)
	}
	assertNoShell(t, c)
}

func TestAnIdentifierThatIsNotTheEnginesOwnFormRunsNothing(t *testing.T) {
	// Install is only ever reached with an entry Available produced, so this is
	// defence in depth -- but it is the depth that matters, because it is the
	// last point before an argv is built.
	//
	// FALSIFYING OBSERVATION: any command running for an identifier this engine
	// could not have produced.
	e, f := aptWith(t, "")
	for _, id := range []string{
		"coreutils",               // no version
		"coreutils@",              // empty version
		"@1.0",                    // no name
		"",                        // nothing
		"coreutils; rm -rf /",     // the interesting one
		"coreutils@1.0 && reboot", // and its cousin
	} {
		err := e.Install(context.Background(), entry{Source: sourceAPT, Identifier: id})
		switch id {
		case "coreutils@1.0 && reboot":
			// This one IS pkg@version-shaped, so it reaches apt-get -- as ONE
			// argv element, where a space is a character and not a separator.
			// The point of the case is that it cannot become two commands.
			if err != nil {
				t.Fatalf("Install(%q): %v", id, err)
			}
		default:
			if err == nil {
				t.Errorf("Install(%q) reported success for an identifier this engine cannot have "+
					"produced", id)
			}
		}
	}
	for _, c := range f.calls {
		assertNoShell(t, c)
		last := c.Args[len(c.Args)-1]
		if strings.Contains(last, ";") || strings.Contains(last, "rm -rf") {
			t.Fatalf("a shell metacharacter reached an argv: %q", c.argv())
		}
	}
}

func TestRebootRequiredIsTheFileDebianItselfWrites(t *testing.T) {
	// FALSIFYING OBSERVATION: the same answer whether the file exists or not,
	// which is the fixture-blind shape this project keeps producing.
	absent := &aptEngine{statFile: func(string) error { return os.ErrNotExist }}
	if absent.RebootRequired(context.Background()) {
		t.Error("reboot_required is true with no /var/run/reboot-required")
	}

	var probed string
	present := &aptEngine{statFile: func(p string) error { probed = p; return nil }}
	if !present.RebootRequired(context.Background()) {
		t.Error("reboot_required is false with /var/run/reboot-required present")
	}
	if probed != rebootRequiredFile {
		t.Errorf("probed %q, want %q", probed, rebootRequiredFile)
	}
}

func TestAptReportsNoInstalledIdentifiersAndThatIsDeliberate(t *testing.T) {
	// A DECISION, pinned so it cannot be "fixed" without reading why. ingest.py
	// resolves every installed identifier that is not also in `patches` with
	// its own SELECT; the apt-equivalent of that list is the whole dpkg
	// database, so reporting it would cost the control plane one query per
	// package per device per scan.
	//
	// FALSIFYING OBSERVATION: a non-empty list, or an error. An error would be
	// worse than the empty list: scan() logs and continues, so it would be a
	// warning on every device forever.
	e, _ := aptWith(t, "")
	got, err := e.Installed(context.Background())
	if err != nil {
		t.Fatalf("Installed returned an error: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("apt reported %d installed identifiers; see the doc comment on "+
			"aptEngine.Installed for why that is a control-plane cost, not a feature", len(got))
	}
}

// ---------------------------------------------------------------------------
// The real thing, on this host
// ---------------------------------------------------------------------------

func TestTheRealAptEngineDescribesThisHost(t *testing.T) {
	// The only end-to-end execution this package gets anywhere. It runs the
	// production runner against the real apt-get on whatever machine CI is,
	// which is what makes the exec seam, the WaitDelay, the bounded buffers and
	// the argv construction more than a design.
	//
	// It asserts SHAPE and not content: the number of outstanding packages on a
	// CI container is not a stable fact.
	if _, err := exec.LookPath(aptGet); err != nil {
		t.Skip("no apt-get on this host")
	}
	eng, err := osEngine(context.Background())
	if err != nil {
		t.Fatalf("osEngine on a host with apt-get: %v", err)
	}
	defer eng.Close()

	if eng.Family() != "linux" {
		t.Fatalf("Family() = %q", eng.Family())
	}

	ctx := context.Background()
	entries, err := eng.Available(ctx)
	if err != nil {
		t.Fatalf("a real apt scan failed: %v", err)
	}
	for _, e := range entries {
		if e.Source != sourceAPT {
			t.Errorf("real entry %+v has the wrong source", e)
		}
		if !strings.Contains(e.Identifier, "@") {
			t.Errorf("real entry %+v is not pkg@version", e)
		}
		if !severities[e.Severity] {
			t.Errorf("real entry %+v carries a severity the server's Literal rejects", e)
		}
	}
	t.Logf("a real apt-get scan of this host produced %d outstanding packages", len(entries))
}

// ---------------------------------------------------------------------------
// Apparatus
// ---------------------------------------------------------------------------

// assertNoShell is the rule stated as an assertion. Rule 2 in exec.go: an argv,
// never a shell, so there is no parser downstream and nothing to escape.
func assertNoShell(t *testing.T, c recordedCall) {
	t.Helper()
	base := filepath.Base(c.Name)
	for _, shell := range []string{"sh", "bash", "zsh", "dash", "cmd", "cmd.exe",
		"powershell", "powershell.exe", "pwsh", "pwsh.exe", "osascript"} {
		if base == shell {
			t.Fatalf("this package invoked %q: %s", shell, c.argv())
		}
	}
}

func containsString(haystack []string, needle string) bool {
	for _, h := range haystack {
		if h == needle {
			return true
		}
	}
	return false
}

func someKeys(m map[string]entry, n int) []string {
	out := make([]string, 0, n)
	for k := range m {
		if len(out) == n {
			break
		}
		out = append(out, k)
	}
	return out
}

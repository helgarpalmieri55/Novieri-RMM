package patch

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// The macOS engine, against a RECONSTRUCTED fixture.
//
// READ testdata/README-fixtures.md BEFORE TRUSTING ANYTHING BELOW. No Mac was
// involved in any of it. `softwareupdate` has never run in this repository, and
// these tests establish that parseSoftwareUpdateList handles the format AS THE
// FIXTURE STATES IT -- not that a real Mac produces that format.
//
// The consequence of being wrong is worth naming, because it is silent: if real
// output does not match, these tests stay green and every Mac in the estate
// reports zero outstanding updates, which is the most dangerous wrong answer
// this capability can give. docs/modules/patch/agent-capabilities.md §6 lists
// it as the top item of what has never executed.

const macFixture = "softwareupdate-list-RECONSTRUCTED.txt"

func macWith(t *testing.T, listing string, respond func(recordedCall) commandResult) (*macEngine, *fakeRunner) {
	t.Helper()
	f := &fakeRunner{respond: func(c recordedCall) commandResult {
		if len(c.Args) > 0 && c.Args[0] == "--list" {
			return commandResult{Stdout: listing}
		}
		if respond != nil {
			return respond(c)
		}
		return commandResult{}
	}}
	return &macEngine{run: f.run, restartOnInstall: map[string]bool{}}, f
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

func TestTheReconstructedSoftwareUpdateListingParsesIntoItsFiveLabels(t *testing.T) {
	// FALSIFYING OBSERVATION: a count or a field value that differs from the
	// fixture. The last label in the fixture has NO attribute line, on purpose:
	// dropping it would silently hide an available update, so the parser must
	// emit it from the label alone.
	entries := parseSoftwareUpdateList(fixture(t, macFixture))
	if len(entries) != 5 {
		got := make([]string, 0, len(entries))
		for _, e := range entries {
			got = append(got, e.entry.Identifier)
		}
		t.Fatalf("parsed %d labels, want 5: %v", len(entries), got)
	}

	byID := map[string]softwareUpdateEntry{}
	for _, e := range entries {
		if e.entry.Source != sourceSoftwareUpdate {
			t.Errorf("%q carries source %q", e.entry.Identifier, e.entry.Source)
		}
		byID[e.entry.Identifier] = e
	}

	// A label WITH SPACES. This is the case that would be a shell-injection
	// question if this engine used a shell; it does not, so a space is a
	// character in one argv element.
	os, ok := byID["macOS Sequoia 15.6.1-24G90"]
	if !ok {
		t.Fatalf("the label with spaces did not survive parsing; got %v", someLabels(byID))
	}
	if os.entry.Title != "macOS Sequoia 15.6.1" {
		t.Errorf("title = %q, want %q (the Version is already inside the Title and must not be "+
			"appended twice)", os.entry.Title, "macOS Sequoia 15.6.1")
	}
	if os.entry.Severity != "important" {
		t.Errorf("Recommended: YES mapped to severity %q, want important", os.entry.Severity)
	}
	if !os.restart {
		t.Error("Action: restart did not set the restart flag; RebootRequired would then report " +
			"false after installing a macOS update, and the control plane would never learn the " +
			"machine needs a restart")
	}

	// Recommended: NO is the OTHER branch, and without it the severity mapping
	// would be a constant that the fixture cannot distinguish from a mapping.
	clt, ok := byID["Command Line Tools for Xcode-16.4"]
	if !ok {
		t.Fatal("the Recommended: NO label is missing from the parse")
	}
	if clt.entry.Severity != "unknown" {
		t.Errorf("Recommended: NO mapped to severity %q, want unknown; PD-02 auto-approves "+
			"critical|important, and Command Line Tools is not a security update",
			clt.entry.Severity)
	}
	if clt.restart {
		t.Error("a label with no Action set the restart flag")
	}

	// Safari: recommended, no restart. The pair (important, no-restart) is what
	// stops `Recommended` and `Action` being read off each other.
	safari, ok := byID["Safari18.6MontereyAuto-18.6"]
	if !ok {
		t.Fatal("the Safari label is missing from the parse")
	}
	if safari.entry.Severity != "important" || safari.restart {
		t.Errorf("Safari parsed as severity=%q restart=%v; the fixture says Recommended: YES with "+
			"no Action, so those two attributes are being read off each other",
			safari.entry.Severity, safari.restart)
	}

	// The trailing label with no attribute line at all.
	itunes, ok := byID["iTunesX-12.8.2"]
	if !ok {
		t.Fatal("a label with no attribute line was dropped; it is still installable and still " +
			"outstanding, so dropping it hides an available update")
	}
	if itunes.entry.Title != "iTunesX-12.8.2" || itunes.entry.Severity != "unknown" {
		t.Errorf("got %+v", itunes.entry)
	}
}

func TestMacSeverityIsAppleWordAndNotAGuess(t *testing.T) {
	for _, tc := range []struct{ in, want string }{
		{"YES", "important"},
		{"yes", "important"},
		{" YES ", "important"},
		{"NO", "unknown"},
		{"", "unknown"},
		{"MAYBE", "unknown"},
	} {
		if got := macSeverity(tc.in); got != tc.want {
			t.Errorf("macSeverity(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestTheListingIsReadFromBothStdoutAndStderr(t *testing.T) {
	// softwareupdate writes its listing to STDERR on several macOS releases and
	// to stdout on others. This is not defensive sloppiness: reading only one
	// is the difference between an engine that works on Ventura and one that
	// reports every Mac in the estate as fully patched.
	//
	// FALSIFYING OBSERVATION: a listing on stderr producing no entries.
	f := &fakeRunner{respond: func(c recordedCall) commandResult {
		return commandResult{Stderr: fixture(t, macFixture)}
	}}
	e := &macEngine{run: f.run, restartOnInstall: map[string]bool{}}

	got, err := e.Available(context.Background())
	if err != nil {
		t.Fatalf("Available: %v", err)
	}
	if len(got) != 5 {
		t.Fatalf("a listing delivered on stderr produced %d entries, want 5", len(got))
	}
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

func TestScanRunsOnlyTheListingAndApplyRunsOnlyTheInstall(t *testing.T) {
	// `--list` alone, and specifically NOT `--list --all-installers` or
	// `--fetch-full-installer`: those enumerate 12 GB macOS installers, and
	// offering a major OS upgrade in the same approval UI as a security fix is
	// how a fleet gets upgraded by accident.
	//
	// FALSIFYING OBSERVATION: any argv other than the two below.
	e, f := macWith(t, fixture(t, macFixture), nil)
	entries, err := e.Available(context.Background())
	if err != nil {
		t.Fatalf("Available: %v", err)
	}
	if got := f.argvs(); len(got) != 1 || got[0] != "/usr/sbin/softwareupdate --list" {
		t.Fatalf("the scan ran %v, want exactly [/usr/sbin/softwareupdate --list]", got)
	}

	var safari entry
	for _, en := range entries {
		if en.Identifier == "Safari18.6MontereyAuto-18.6" {
			safari = en
		}
	}
	if err := e.Install(context.Background(), safari); err != nil {
		t.Fatalf("Install: %v", err)
	}
	last := f.calls[len(f.calls)-1]
	want := "/usr/sbin/softwareupdate --install Safari18.6MontereyAuto-18.6"
	if last.argv() != want {
		t.Fatalf("the install ran\n  %s\nwant\n  %s", last.argv(), want)
	}
	// The label is ONE argv element, whatever is in it, and there are exactly
	// two: softwareupdate has NO `--no-restart` flag (not restarting is its
	// default; `-R` opts in), and an invented flag would make every macOS
	// install fail with a usage error that nothing in this CI could show.
	if len(last.Args) != 2 {
		t.Fatalf("the install argv has %d elements (%v); it must be exactly --install and the "+
			"label", len(last.Args), last.Args)
	}
	for _, a := range last.Args {
		if a == "--restart" || a == "-R" || a == "--no-restart" {
			t.Fatalf("the install argv carries %q: -R/--restart would have the agent reboot a "+
				"machine the control plane cannot be told about, and --no-restart is not a flag "+
				"softwareupdate has", a)
		}
	}
	for _, c := range f.calls {
		assertNoShell(t, c)
	}
}

func TestALabelWithSpacesStaysOneArgumentAndNoShellIsInvolved(t *testing.T) {
	// "macOS Sequoia 15.6.1-24G90" contains two spaces. Through a shell that is
	// three words; through exec.Command's argv it is one string, which is why
	// this engine never composes a command line.
	//
	// FALSIFYING OBSERVATION: the label split across argv elements.
	e, f := macWith(t, fixture(t, macFixture), nil)
	entries, _ := e.Available(context.Background())
	var osUpdate entry
	for _, en := range entries {
		if strings.HasPrefix(en.Identifier, "macOS Sequoia") {
			osUpdate = en
		}
	}
	if osUpdate.Identifier == "" {
		t.Fatal("the multi-word label is not in the parse")
	}
	if err := e.Install(context.Background(), osUpdate); err != nil {
		t.Fatalf("Install: %v", err)
	}
	last := f.calls[len(f.calls)-1]
	if last.Args[1] != "macOS Sequoia 15.6.1-24G90" {
		t.Fatalf("argv[1] is %q; the label was split or mangled: %v", last.Args[1], last.Args)
	}
	assertNoShell(t, last)
}

func TestRebootRequiredFollowsWhatWasActuallyInstalled(t *testing.T) {
	// THE FIXTURE-BLINDNESS CHECK FOR THIS ENGINE. Installing Safari (no
	// restart) and installing the OS update (restart) must produce DIFFERENT
	// answers; an engine that always said true, or always false, would satisfy
	// a single-case test.
	//
	// FALSIFYING OBSERVATION: the same reboot answer for both.
	install := func(id string) bool {
		t.Helper()
		e, _ := macWith(t, fixture(t, macFixture), nil)
		entries, err := e.Available(context.Background())
		if err != nil {
			t.Fatalf("Available: %v", err)
		}
		for _, en := range entries {
			if en.Identifier == id {
				if err := e.Install(context.Background(), en); err != nil {
					t.Fatalf("Install(%s): %v", id, err)
				}
				return e.RebootRequired(context.Background())
			}
		}
		t.Fatalf("%s is not in the parse", id)
		return false
	}

	if install("Safari18.6MontereyAuto-18.6") {
		t.Error("installing an update with no Action: restart reported reboot_required")
	}
	if !install("macOS Sequoia 15.6.1-24G90") {
		t.Error("installing an update flagged Action: restart did NOT report reboot_required; the " +
			"control plane would never learn the machine needs one, and PatchApplyResult has no " +
			"other member that could carry it")
	}
}

func TestSoftwareUpdatesOwnClosingSentenceAlsoSetsRebootRequired(t *testing.T) {
	// An update can decide DURING installation that it needs a restart even
	// when the catalog did not say so. Belt and braces for the Action flag.
	e, _ := macWith(t, fixture(t, macFixture), func(c recordedCall) commandResult {
		return commandResult{Stdout: "Done.\nYou have installed one or more updates that " +
			"requires that you restart your computer.  Please restart immediately.\n"}
	})
	entries, _ := e.Available(context.Background())
	for _, en := range entries {
		if en.Identifier == "Safari18.6MontereyAuto-18.6" {
			if err := e.Install(context.Background(), en); err != nil {
				t.Fatalf("Install: %v", err)
			}
		}
	}
	if !e.RebootRequired(context.Background()) {
		t.Error("softwareupdate said the machine must be restarted and RebootRequired reported " +
			"false; only the catalog's Action flag is being read")
	}
}

func TestAFailedListingIsAPlainErrorAndNotARefusal(t *testing.T) {
	// Same reasoning as apt's: a Mac that could not reach Apple's catalog this
	// time is "try again", not "this agent will never do this".
	f := &fakeRunner{respond: func(c recordedCall) commandResult {
		return commandResult{ExitCode: 1, Stderr: "Software Update Tool\n\nNo new software available.\n" +
			"Error: could not contact the update server", Err: errors.New("exit status 1")}
	}}
	e := &macEngine{run: f.run, restartOnInstall: map[string]bool{}}

	_, err := e.Available(context.Background())
	if err == nil {
		t.Fatal("a failed listing returned no error")
	}
	var refusal *jobs.Refusal
	if errors.As(err, &refusal) {
		t.Fatalf("an unreachable Apple catalog was REFUSED: %q", refusal.Reason)
	}
}

func TestMacReportsNoInstalledIdentifiersAndThatIsDeliberate(t *testing.T) {
	// A DECISION, pinned. `softwareupdate --history` is a fixed-width table of
	// display names with no relationship to the `--list` labels this engine
	// uses as identifiers, so a parser over it would produce identifiers that
	// match nothing in the catalog -- one wasted SELECT each, and a parser
	// written against a format nobody here has seen would LOOK like coverage.
	e, _ := macWith(t, "", nil)
	got, err := e.Installed(context.Background())
	if err != nil || len(got) != 0 {
		t.Fatalf("Installed = %v, %v; want empty and no error", got, err)
	}
}

func someLabels(m map[string]softwareUpdateEntry) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

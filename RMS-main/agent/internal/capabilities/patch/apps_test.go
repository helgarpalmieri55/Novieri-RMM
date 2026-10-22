package patch

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"
)

// A fake app source.
type fakeApp struct {
	source    string
	outdated  []entry
	err       error
	upgrades  []entry
	upErr     error
	restarted bool
}

func (f *fakeApp) Source() string { return f.source }
func (f *fakeApp) Outdated(context.Context) ([]entry, error) {
	return f.outdated, f.err
}
func (f *fakeApp) Upgrade(_ context.Context, e entry) error {
	f.upgrades = append(f.upgrades, e)
	return f.upErr
}
func (f *fakeApp) RestartNeeded() bool { return f.restarted }

func appService(primary *fakeEngine, slots ...appSlot) *Service {
	eng := newWithApps(primary, slots...)
	return New(Options{
		NewEngine:    func(context.Context) (engine, error) { return eng, nil },
		Clock:        func() time.Time { return testEpoch },
		Logger:       quiet(),
		BootTime:     func(time.Time) (time.Time, bool) { return time.Time{}, false },
		NewRestarter: func() restarter { return &fakeRestarter{} },
	})
}

func TestParseWingetUpgradeReadsTheTableAndStopsAtItsEnd(t *testing.T) {
	raw, err := os.ReadFile("testdata/winget-upgrade-RECONSTRUCTED.txt")
	if err != nil {
		t.Fatal(err)
	}
	got := parseWingetUpgrade(strings.ReplaceAll(string(raw), "\n", "\r\n"))
	ids := make([]string, 0, len(got))
	for _, e := range got {
		ids = append(ids, e.Identifier)
		if e.Source != sourceWinget || e.Severity != "unknown" {
			t.Errorf("%s: source=%q severity=%q", e.Identifier, e.Source, e.Severity)
		}
	}
	// The truncated id is skipped, not guessed; the pinned table is not read.
	want := []string{"Mozilla.Firefox", "Google.Chrome", "Microsoft.VCRedist.2015+.x64", "7zip.7zip", "Zoom.Zoom"}
	if !reflect.DeepEqual(ids, want) {
		t.Fatalf("ids = %v, want %v", ids, want)
	}
	if got[0].Title != "Mozilla Firefox (x64 en-US) 130.0 → 131.0.2" {
		t.Errorf("title = %q", got[0].Title)
	}
	if got[3].Title != "7-Zip 23.01 (x64) < 23.01 → 24.08" {
		t.Errorf("title = %q", got[3].Title)
	}
}

func TestParseWingetUpgradeWithNothingToDo(t *testing.T) {
	if got := parseWingetUpgrade("No installed package found matching input criteria.\r\n"); got != nil {
		t.Fatalf("got %v", got)
	}
}

func TestWingetUpgradeUsesTheListedIDAndFixedFlags(t *testing.T) {
	var calls [][]string
	w := &wingetEngine{path: `C:\WindowsApps\winget.exe`, run: func(_ context.Context, _ []string, name string, args ...string) commandResult {
		calls = append(calls, append([]string{name}, args...))
		return commandResult{ExitCode: int(0x8A150109), Err: errors.New("exit")}
	}}
	if err := w.Upgrade(context.Background(), entry{Source: sourceWinget, Identifier: "Mozilla.Firefox"}); err != nil {
		t.Fatalf("a restart-to-finish result is an install: %v", err)
	}
	if !w.RestartNeeded() {
		t.Fatal("the restart-to-finish result was not remembered")
	}
	want := []string{`C:\WindowsApps\winget.exe`, "upgrade", "--id", "Mozilla.Firefox", "--exact",
		"--silent", "--accept-package-agreements", "--disable-interactivity", "--accept-source-agreements"}
	if !reflect.DeepEqual(calls[0], want) {
		t.Fatalf("argv = %q", calls[0])
	}

	w.run = func(context.Context, []string, string, ...string) commandResult {
		return commandResult{ExitCode: int(0x8A15002B), Err: errors.New("exit"), Stdout: "No applicable upgrade found."}
	}
	err := w.Upgrade(context.Background(), entry{Source: sourceWinget, Identifier: "Zoom.Zoom"})
	if err == nil || !strings.Contains(err.Error(), "0x8A15002B") || !strings.Contains(err.Error(), "No applicable") {
		t.Fatalf("err = %v", err)
	}
}

func TestAppSourcesAreReportedAndNeverCostTheOSScan(t *testing.T) {
	primary := kb1Engine(false)
	ok := &fakeApp{source: sourceWinget, outdated: []entry{{Source: sourceWinget, Identifier: "Google.Chrome", Title: "Chrome", Severity: "unknown"}}}
	svc := appService(primary, appSlot{source: sourceWinget, eng: ok})
	doc, err := svc.scan(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(doc.Patches) != 2 || doc.Patches[1].Source != sourceWinget {
		t.Fatalf("patches = %+v", doc.Patches)
	}
	if len(doc.AppSources) != 1 || doc.AppSources[0].State != "scanned" {
		t.Fatalf("app_sources = %+v", doc.AppSources)
	}

	failing := &fakeApp{source: sourceWinget, err: errors.New("source agreement\x1b[31m")}
	doc, err = appService(kb1Engine(false), appSlot{source: sourceWinget, eng: failing}).scan(context.Background())
	if err != nil || len(doc.Patches) != 1 {
		t.Fatalf("a failed app source cost the OS scan: err=%v patches=%v", err, doc.Patches)
	}
	if doc.AppSources[0].State != "failed" || strings.ContainsRune(*doc.AppSources[0].Detail, 0x1b) {
		t.Fatalf("app_sources = %+v", doc.AppSources[0])
	}

	doc, _ = appService(kb1Engine(false),
		appSlot{source: sourceHomebrew, unsupported: "Homebrew is not installed"}).scan(context.Background())
	if doc.AppSources[0].State != "unsupported" || *doc.AppSources[0].Detail != "Homebrew is not installed" {
		t.Fatalf("app_sources = %+v", doc.AppSources[0])
	}
}

func TestAnOlderControlPlaneIsSentNoAppUpdates(t *testing.T) {
	app := &fakeApp{source: sourceWinget, outdated: []entry{{Source: sourceWinget, Identifier: "Google.Chrome", Title: "Chrome", Severity: "unknown"}}}
	doc, err := appService(kb1Engine(false), appSlot{source: sourceWinget, eng: app}).scan(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(doc.forVersion(1))
	if strings.Contains(string(raw), "Google.Chrome") || strings.Contains(string(raw), "app_sources") {
		t.Fatalf("v1 document = %s", raw)
	}
}

func TestApplyRoutesAppUpdatesToTheirManagerAndOnlyWhatItListed(t *testing.T) {
	app := &fakeApp{source: sourceWinget, outdated: []entry{{Source: sourceWinget, Identifier: "Google.Chrome", Title: "Chrome", Severity: "unknown"}}, restarted: true}
	primary := kb1Engine(false)
	svc := appService(primary, appSlot{source: sourceWinget, eng: app})
	raw, _ := json.Marshal(map[string]any{
		"reboot": "never",
		"patches": []map[string]string{
			ref(sourceWindowsUpdate, "KB1"),
			ref(sourceWinget, "Google.Chrome"),
			ref(sourceWinget, "Evil.App --override \"/S /D=C:\\\""),
		},
	})
	res, err := svc.apply(context.Background(), raw)
	if err != nil {
		t.Fatal(err)
	}
	if len(app.upgrades) != 1 || app.upgrades[0].Identifier != "Google.Chrome" || app.upgrades[0].Title != "Chrome" {
		t.Fatalf("upgrades = %+v", app.upgrades)
	}
	if len(primary.installs) != 1 || primary.installs[0].Identifier != "KB1" {
		t.Fatalf("OS installs = %+v", primary.installs)
	}
	if res.Results[2].State != "failed" {
		t.Fatalf("an id winget never listed was reported %s", res.Results[2].State)
	}
	if !res.RebootRequired {
		t.Fatal("an app upgrade asked for a restart and the result says none is needed")
	}
}

func TestParseBrewOutdatedReadsCasksOnly(t *testing.T) {
	got, err := parseBrewOutdated(`{"formulae":[{"name":"wget"}],"casks":[
		{"name":"firefox","installed_versions":["130.0"],"current_version":"131.0"},
		{"name":"../../evil","installed_versions":["1"],"current_version":"2"}]}`)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Identifier != "firefox" || got[0].Source != sourceHomebrew ||
		got[0].Title != "firefox 130.0 → 131.0" {
		t.Fatalf("got %+v", got)
	}
	if _, err := parseBrewOutdated("Error: Running Homebrew as root is extremely dangerous"); err == nil {
		t.Fatal("non-JSON output must be an error, not an empty list")
	}
}

func TestBrewRunsUpdateOnceThenListsAndUpgradesByToken(t *testing.T) {
	var calls [][]string
	b := &brewEngine{run: func(_ context.Context, args ...string) commandResult {
		calls = append(calls, args)
		if args[0] == "outdated" {
			return commandResult{Stdout: `{"casks":[]}`}
		}
		return commandResult{}
	}}
	for i := 0; i < 2; i++ {
		if _, err := b.Outdated(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	if err := b.Upgrade(context.Background(), entry{Source: sourceHomebrew, Identifier: "firefox"}); err != nil {
		t.Fatal(err)
	}
	want := [][]string{
		{"update", "--quiet"},
		{"outdated", "--cask", "--json=v2"},
		{"outdated", "--cask", "--json=v2"},
		{"upgrade", "--cask", "firefox"},
	}
	if !reflect.DeepEqual(calls, want) {
		t.Fatalf("calls = %q", calls)
	}
}

func TestTheAppSourceSchemaMatches(t *testing.T) {
	s := loadSchema(t, "PatchScanDocument")
	assertShape(t, def(t, s, "AppSourceStatus"), reflect.TypeOf(appSource{}), nil)
	got := enumStrings(t, def(t, s, "AppSourceStatus").Properties["state"])
	want := map[string]bool{"scanned": true, "unsupported": true, "failed": true}
	if len(got) != len(want) {
		t.Fatalf("states = %v", got)
	}
	for _, g := range got {
		if !want[g] {
			t.Fatalf("states = %v", got)
		}
	}
}

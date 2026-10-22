package install

import (
	"errors"
	"os"
	"reflect"
	"regexp"
	"strings"
	"testing"
	"time"
)

const (
	msiExe          = `C:\Program Files\ITSuite\its-agent.exe`
	msiSettingsPath = `C:\Program Files\ITSuite\msi-enroll.json`
	msiCode         = "AbCdEfGhIjKlMnOpQrStUvWxYz012_-9"
)

var msiGood = []byte(`{"console":"https://itms.example.com","code":"` + msiCode + `"}`)

type fakeMsi struct {
	elevated, enrolled             bool
	settings                       []byte
	readErr, installErr, removeErr error
	calls                          []string
	plan                           Plan
	code                           string
	logs                           []string // "<dir>|<line>"
}

func (f *fakeMsi) Log(dir, line string) { f.logs = append(f.logs, dir+"|"+line) }

func (f *fakeMsi) Elevated() bool { f.calls = append(f.calls, "elevated?"); return f.elevated }
func (f *fakeMsi) Enrolled(p Plan) bool {
	f.calls = append(f.calls, "enrolled? "+p.DataDir)
	return f.enrolled
}
func (f *fakeMsi) EnsureRunning(name string) error {
	f.calls = append(f.calls, "ensure-running "+name)
	return nil
}
func (f *fakeMsi) ReadSettings(path string, limit int64) ([]byte, error) {
	f.calls = append(f.calls, "read "+path)
	if limit != maxMsiSettingsBytes {
		return nil, errors.New("unbounded read")
	}
	return f.settings, f.readErr
}
func (f *fakeMsi) Install(p Plan, code string) error {
	f.calls = append(f.calls, "install")
	f.plan, f.code = p, code
	return f.installErr
}
func (f *fakeMsi) Remove(path string) error {
	f.calls = append(f.calls, "remove "+path)
	return f.removeErr
}

func TestMsiInstallJoinsThroughTheSettingsFileAndDeletesIt(t *testing.T) {
	sys := &fakeMsi{elevated: true, settings: msiGood}
	if err := msiInstall(sys, msiExe, `C:\Program Files`, `C:\ProgramData`); err != nil {
		t.Fatal(err)
	}
	want := []string{"elevated?", `enrolled? C:\ProgramData\ITSuite\agent`, "read " + msiSettingsPath, "install", "remove " + msiSettingsPath}
	if !reflect.DeepEqual(sys.calls, want) {
		t.Fatalf("calls = %v\nwant %v", sys.calls, want)
	}
	if sys.code != msiCode || sys.plan.Target != msiExe {
		t.Fatalf("installed %q to %q", sys.code, sys.plan.Target)
	}
	env := strings.Join(sys.plan.Environment, "|")
	if !strings.Contains(env, "ITS_AGENT_ENROLL_URL=https://itms.example.com") || strings.Contains(env, msiCode) {
		t.Fatalf("service environment = %q", env)
	}
}

func TestMsiInstallRefusesUnlessElevated(t *testing.T) {
	sys := &fakeMsi{settings: msiGood}
	err := msiInstall(sys, msiExe, `C:\Program Files`, `C:\ProgramData`)
	if err == nil || !strings.Contains(err.Error(), "SYSTEM") || !reflect.DeepEqual(sys.calls, []string{"elevated?"}) {
		t.Fatalf("err=%v calls=%v", err, sys.calls)
	}
}

func TestMsiInstallSetsUpOnlyTheBinaryTheMsiPlaced(t *testing.T) {
	sys := &fakeMsi{elevated: true, settings: msiGood}
	err := msiInstall(sys, `C:\Users\Public\its-agent.exe`, `C:\Program Files`, `C:\ProgramData`)
	if err == nil || len(sys.calls) != 1 {
		t.Fatalf("err=%v calls=%v; a binary outside Program Files\\ITSuite must be refused before anything is read", err, sys.calls)
	}
	// Windows paths are case-insensitive, and the MSI may launch either way.
	sys = &fakeMsi{elevated: true, settings: msiGood}
	if err := msiInstall(sys, `c:\program files\itsuite\ITS-AGENT.EXE`, `C:\Program Files`, `C:\ProgramData`); err != nil {
		t.Fatal(err)
	}
}

func TestMsiInstallChangesNothingWhenAlreadyEnrolled(t *testing.T) {
	sys := &fakeMsi{elevated: true, enrolled: true}
	if err := msiInstall(sys, msiExe, `C:\Program Files`, `C:\ProgramData`); err != nil {
		t.Fatal(err)
	}
	want := []string{"elevated?", `enrolled? C:\ProgramData\ITSuite\agent`, "remove " + msiSettingsPath, "ensure-running its-agent"}
	if !reflect.DeepEqual(sys.calls, want) {
		t.Fatalf("calls = %v\nwant %v", sys.calls, want)
	}
}

func TestMsiInstallFailsWhenTheSettingsFileCannotBeDeleted(t *testing.T) {
	sys := &fakeMsi{elevated: true, settings: msiGood, removeErr: os.ErrPermission}
	err := msiInstall(sys, msiExe, `C:\Program Files`, `C:\ProgramData`)
	if err == nil || !strings.Contains(err.Error(), "could not be deleted") {
		t.Fatalf("err = %v", err)
	}
	// An already-deleted file is not a failure.
	sys = &fakeMsi{elevated: true, settings: msiGood, removeErr: os.ErrNotExist}
	if err := msiInstall(sys, msiExe, `C:\Program Files`, `C:\ProgramData`); err != nil {
		t.Fatal(err)
	}
}

func TestMsiInstallStopsAtAFailedInstallWithoutDeletingTheSettings(t *testing.T) {
	sys := &fakeMsi{elevated: true, settings: msiGood, installErr: errors.New("refused by the fake")}
	if err := msiInstall(sys, msiExe, `C:\Program Files`, `C:\ProgramData`); err == nil {
		t.Fatal("a failed install was reported as success")
	}
	if last := sys.calls[len(sys.calls)-1]; last != "install" {
		t.Fatalf("after a failed install: %v", sys.calls)
	}
}

func TestMsiSettingsAreValidated(t *testing.T) {
	good, err := parseMsiSettings(msiGood)
	if err != nil || good != (Link{Console: "https://itms.example.com", Code: msiCode}) {
		t.Fatalf("good settings: %+v, %v", good, err)
	}
	if l, err := parseMsiSettings([]byte(`{"console":"https://itms.example.com:8443","code":"` + msiCode + `"}`)); err != nil || l.Console != "https://itms.example.com:8443" {
		t.Fatalf("a port: %+v, %v", l, err)
	}
	for name, bad := range map[string]string{
		"not json":        `console=https://itms.example.com`,
		"empty":           ``,
		"another member":  `{"console":"https://itms.example.com","code":"` + msiCode + `","url":"x"}`,
		"two values":      string(msiGood) + ` {}`,
		"http":            `{"console":"http://itms.example.com","code":"` + msiCode + `"}`,
		"a path":          `{"console":"https://itms.example.com/i/x","code":"` + msiCode + `"}`,
		"no console":      `{"code":"` + msiCode + `"}`,
		"short code":      `{"console":"https://itms.example.com","code":"abc"}`,
		"long code":       `{"console":"https://itms.example.com","code":"` + msiCode + `x"}`,
		"bad character":   `{"console":"https://itms.example.com","code":"` + msiCode[:31] + `!"}`,
		"code not string": `{"console":"https://itms.example.com","code":42}`,
		"too large":       `{"console":"https://itms.example.com","code":"` + msiCode + `"}` + strings.Repeat(" ", maxMsiSettingsBytes),
	} {
		_, err := parseMsiSettings([]byte(bad))
		if err == nil {
			t.Errorf("%s: accepted %q", name, bad)
			continue
		}
		if strings.Contains(err.Error(), msiCode[:20]) {
			t.Errorf("%s: the error carries the code: %v", name, err)
		}
	}
}

var msiNow = time.Date(2026, 9, 30, 10, 11, 12, 0, time.FixedZone("CEST", 2*60*60))

// ONE LINE PER OUTCOME in %ProgramData%\ITSuite\msi.log: the timestamp (UTC),
// the mode, and ok or the reason (review item 12).
func TestMsiInstallLogsOneLinePerOutcome(t *testing.T) {
	sys := &fakeMsi{elevated: true, settings: msiGood}
	if err := runMsiInstall(sys, msiExe, `C:\Program Files`, `D:\ProgramData\`, msiNow); err != nil {
		t.Fatal(err)
	}
	if want := []string{`D:\ProgramData\ITSuite|2026-09-30T08:11:12Z --msi-install ok`}; !reflect.DeepEqual(sys.logs, want) {
		t.Fatalf("logs = %q, want %q", sys.logs, want)
	}

	sys = &fakeMsi{settings: msiGood}
	if err := runMsiInstall(sys, msiExe, `C:\Program Files`, `C:\ProgramData`, msiNow); err == nil {
		t.Fatal("an unelevated run succeeded")
	}
	if len(sys.logs) != 1 || !strings.HasPrefix(sys.logs[0], `C:\ProgramData\ITSuite|2026-09-30T08:11:12Z --msi-install failed: --msi-install must run as SYSTEM`) {
		t.Fatalf("logs = %q", sys.logs)
	}
}

// Whatever fails, the line carries neither the code nor the settings file.
func TestMsiLogNeverCarriesTheCodeOrTheSettings(t *testing.T) {
	cases := map[string]*fakeMsi{
		"install fails":        {elevated: true, settings: msiGood, installErr: errors.New("refused by the fake")},
		"settings not deleted": {elevated: true, settings: msiGood, removeErr: os.ErrPermission},
		"unreadable settings":  {elevated: true, readErr: os.ErrPermission},
		"http console":         {elevated: true, settings: []byte(`{"console":"http://itms.example.com","code":"` + msiCode + `"}`)},
		"a path":               {elevated: true, settings: []byte(`{"console":"https://itms.example.com/i/` + msiCode + `","code":"` + msiCode + `"}`)},
		"short code":           {elevated: true, settings: []byte(`{"console":"https://itms.example.com","code":"` + msiCode[:31] + `"}`)},
		"another member":       {elevated: true, settings: []byte(`{"console":"https://itms.example.com","code":"` + msiCode + `","x":"` + msiCode + `"}`)},
		"not json":             {elevated: true, settings: []byte(msiCode)},
		"success":              {elevated: true, settings: msiGood},
		"already enrolled":     {elevated: true, enrolled: true, settings: msiGood},
	}
	for name, sys := range cases {
		_ = runMsiInstall(sys, msiExe, `C:\Program Files`, `C:\ProgramData`, msiNow)
		if len(sys.logs) != 1 {
			t.Errorf("%s: %d lines, want 1", name, len(sys.logs))
			continue
		}
		line := sys.logs[0]
		if strings.Contains(line, msiCode[:12]) || strings.Contains(line, "itms.example.com") || strings.Contains(line, `"console"`) {
			t.Errorf("%s: the line carries the settings: %q", name, line)
		}
	}
}

func TestMsiLogLineIsOneBoundedLine(t *testing.T) {
	line := msiLogLine(msiNow, MsiInstallArg, errors.New("first\r\nsecond\tthird "+strings.Repeat("x", 2000)))
	if strings.ContainsAny(line, "\r\n\t") {
		t.Fatalf("the line spans lines: %q", line)
	}
	if !regexp.MustCompile(`^2026-09-30T08:11:12Z --msi-install failed: first second third x+\.\.\.$`).MatchString(line) {
		t.Fatalf("line = %q", line)
	}
	if len(line) > 600 {
		t.Fatalf("the line is %d bytes; the reason must be bounded", len(line))
	}
	if MsiLogFile != "msi.log" {
		t.Fatalf("MsiLogFile = %q; deploy/WINDOWS-PUSH.md §7 names msi.log", MsiLogFile)
	}
}

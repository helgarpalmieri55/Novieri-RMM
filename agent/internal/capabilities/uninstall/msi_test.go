package uninstall

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

// An MSI removal (GPO MSI spec §2.3): the service and the data go; the binary
// and its directory stay for Windows Installer, which owns them; nothing waits
// and nothing is scheduled for the next boot.
func TestAnMsiRemovalLeavesTheBinaryToWindowsInstaller(t *testing.T) {
	tg := layout(t)
	sys := &fakeSystem{}
	if err := RunForMSI(context.Background(), sys, tg, ""); err != nil {
		t.Fatal(err)
	}
	if want := []string{"stop novieri-agent", "purge", "delete novieri-agent"}; !reflect.DeepEqual(sys.calls, want) {
		t.Fatalf("calls = %v, want %v (no wait, no boot-time delete, no helper exit)", sys.calls, want)
	}
	gone(t, tg.DataDir, tg.Remove[0])
	if _, err := os.Stat(tg.Binary); err != nil {
		t.Fatalf("the binary is the MSI's to remove, and it went: %v", err)
	}
}

// Every step still runs when one fails, and the failure is reported.
func TestAnMsiRemovalGoesOnPastAFailureAndReportsIt(t *testing.T) {
	tg := layout(t)
	sys := &fakeSystem{stopErr: os.ErrPermission}
	if err := RunForMSI(context.Background(), sys, tg, ""); err == nil {
		t.Fatal("a failed stop was not reported")
	}
	gone(t, tg.DataDir)
}

// A data directory that holds no identity keeps everything but the named
// credentials, exactly as the signed uninstall does (review M3).
func TestAnMsiRemovalLeavesADataDirectoryThatIsNotAnAgents(t *testing.T) {
	tg := layout(t)
	if err := os.Remove(filepath.Join(tg.DataDir, "identity.json")); err != nil {
		t.Fatal(err)
	}
	if err := RunForMSI(context.Background(), &fakeSystem{}, tg, ""); err == nil {
		t.Fatal("removing only the credentials was not reported")
	}
	if _, err := os.Stat(filepath.Join(tg.DataDir, "update")); err != nil {
		t.Fatalf("a directory that may not be an agent's was emptied: %v", err)
	}
	gone(t, filepath.Join(tg.DataDir, "agent.key"))
}

func TestAnMsiRemovalRefusesTargetsThatAreNotAnAgent(t *testing.T) {
	tg := layout(t)
	tg.Binary = filepath.Join(filepath.Dir(tg.Binary), "explorer.exe")
	sys := &fakeSystem{}
	if err := RunForMSI(context.Background(), sys, tg, ""); err == nil || len(sys.calls) != 0 {
		t.Fatalf("err=%v calls=%v; targets that are not an agent must be refused before anything runs", err, sys.calls)
	}
}

func TestMsiTargetsAreTheWindowsLayout(t *testing.T) {
	got := msiTargets(`D:\ProgramData\`, `C:\Program Files\NovieriRMM\novieri-agent.exe`)
	if got.DataDir != `D:\ProgramData\NovieriRMM\agent` || got.Binary != `C:\Program Files\NovieriRMM\novieri-agent.exe` ||
		got.ServiceName != "novieri-agent" {
		t.Fatalf("msiTargets = %+v", got)
	}
	// %ProgramData%\NovieriRMM goes if it is empty; %ProgramFiles%\NovieriRMM is
	// the MSI's own directory, which Windows Installer removes.
	if !reflect.DeepEqual(got.RemoveIfEmpty, []string{`D:\ProgramData\NovieriRMM`}) {
		t.Fatalf("RemoveIfEmpty = %v", got.RemoveIfEmpty)
	}
	if msiLogDir("") != `C:\ProgramData\NovieriRMM` || msiLogDir(`D:\ProgramData\`) != `D:\ProgramData\NovieriRMM` {
		t.Fatalf("msiLogDir = %q, %q", msiLogDir(""), msiLogDir(`D:\ProgramData\`))
	}
	if def := msiTargets("", `C:\x\novieri-agent.exe`); def.DataDir != `C:\ProgramData\NovieriRMM\agent` {
		t.Fatalf("default ProgramData: %+v", def)
	}
}

// msiLayout is layout() under an NovieriRMM directory, as an MSI installs it:
// <root>/NovieriRMM/agent holding the data, <root>/NovieriRMM/msi.log the install's
// line, and NovieriRMM in RemoveIfEmpty.
func msiLayout(t *testing.T) (Targets, string) {
	t.Helper()
	tg := layout(t)
	suite := filepath.Join(filepath.Dir(tg.DataDir), "NovieriRMM")
	if err := os.Mkdir(suite, 0o700); err != nil {
		t.Fatal(err)
	}
	data := filepath.Join(suite, "agent")
	if err := os.Rename(tg.DataDir, data); err != nil {
		t.Fatal(err)
	}
	tg.DataDir = data
	tg.RemoveIfEmpty = []string{suite}
	if err := os.WriteFile(filepath.Join(suite, MsiLogFile), []byte("2026-09-30T08:00:00Z --msi-install ok\r\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return tg, suite
}

// A clean removal leaves nothing: the install's msi.log, then the empty
// %ProgramData%\NovieriRMM, go too (review item 14).
func TestAnMsiRemovalRemovesAnEmptyNovieriRMM(t *testing.T) {
	tg, suite := msiLayout(t)
	if err := RunForMSI(context.Background(), &fakeSystem{}, tg, filepath.Join(suite, MsiLogFile)); err != nil {
		t.Fatal(err)
	}
	gone(t, tg.DataDir, suite)
}

func TestAnMsiRemovalKeepsAnNovieriRMMThatHoldsSomethingElse(t *testing.T) {
	tg, suite := msiLayout(t)
	other := filepath.Join(suite, "push")
	if err := os.Mkdir(other, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := RunForMSI(context.Background(), &fakeSystem{}, tg, filepath.Join(suite, MsiLogFile)); err != nil {
		t.Fatal(err)
	}
	gone(t, tg.DataDir, filepath.Join(suite, MsiLogFile))
	if _, err := os.Stat(other); err != nil {
		t.Fatalf("something else in NovieriRMM was removed: %v", err)
	}
}

// A removal that failed keeps msi.log, so its reason can be added to it.
func TestAFailedMsiRemovalKeepsTheLog(t *testing.T) {
	tg, suite := msiLayout(t)
	if err := RunForMSI(context.Background(), &fakeSystem{stopErr: os.ErrPermission}, tg, filepath.Join(suite, MsiLogFile)); err == nil {
		t.Fatal("a failed stop was not reported")
	}
	if _, err := os.Stat(filepath.Join(suite, MsiLogFile)); err != nil {
		t.Fatalf("msi.log went after a failed removal: %v", err)
	}
}

var msiNow = time.Date(2026, 9, 30, 10, 11, 12, 0, time.FixedZone("CEST", 2*60*60))

// --msi-uninstall gets --msi-install's elevation refusal (review item 11):
// nothing is stopped, deleted or removed, and the refusal is logged.
func TestMsiUninstallRefusesUnlessElevated(t *testing.T) {
	tg, suite := msiLayout(t)
	sys := &fakeSystem{}
	var lines []string
	err := msiUninstall(context.Background(), false, sys, tg, suite, func(dir, line string) {
		lines = append(lines, dir+"|"+line)
	}, msiNow)
	if err == nil || !strings.Contains(err.Error(), "SYSTEM or an elevated administrator") {
		t.Fatalf("err = %v", err)
	}
	if len(sys.calls) != 0 {
		t.Fatalf("calls = %v; an unelevated removal must change nothing", sys.calls)
	}
	for _, still := range []string{tg.DataDir, filepath.Join(tg.DataDir, "identity.json"), filepath.Join(suite, MsiLogFile)} {
		if _, err := os.Stat(still); err != nil {
			t.Fatalf("%s went on a refused removal: %v", still, err)
		}
	}
	want := []string{suite + "|2026-09-30T08:11:12Z --msi-uninstall failed: " + notElevated}
	if !reflect.DeepEqual(lines, want) {
		t.Fatalf("lines = %q\nwant %q", lines, want)
	}
}

// A clean removal writes NO line: it has just deleted msi.log on purpose, and
// the real log writer (secdir.AppendLine) creates the file when the directory
// is still there -- which it is whenever %ProgramData%\NovieriRMM holds something
// that is not the MSI's. windows-latest showed exactly that on 2026-09-30:
// "msi.log survived msiexec /x". Only a failure is logged, with its reason.
func TestMsiUninstallLogsOnlyAFailure(t *testing.T) {
	tg, suite := msiLayout(t)
	var lines []string
	log := func(dir, line string) { lines = append(lines, dir+"|"+line) }
	if err := msiUninstall(context.Background(), true, &fakeSystem{}, tg, suite, log, msiNow); err != nil {
		t.Fatal(err)
	}
	if len(lines) != 0 {
		t.Fatalf("a clean removal wrote %q; it must write nothing", lines)
	}

	tg, suite = msiLayout(t)
	lines = nil
	if err := msiUninstall(context.Background(), true, &fakeSystem{stopErr: os.ErrPermission}, tg, suite, log, msiNow); err == nil {
		t.Fatal("a failed stop was not reported")
	}
	if len(lines) != 1 || !strings.HasPrefix(lines[0], suite+"|2026-09-30T08:11:12Z --msi-uninstall failed: ") ||
		strings.ContainsAny(lines[0], "\r\n") {
		t.Fatalf("lines = %q", lines)
	}
}

// The runner's case end to end: NovieriRMM also holds something else, so it
// stays, and a log writer that creates a missing file (as AppendLine's
// OPEN_ALWAYS does) must still leave no msi.log after a clean removal.
func TestACleanMsiRemovalLeavesNoLogEvenWhenNovieriRMMStays(t *testing.T) {
	tg, suite := msiLayout(t)
	if err := os.Mkdir(filepath.Join(suite, "uninstall-1"), 0o700); err != nil {
		t.Fatal(err)
	}
	appendCreating := func(dir, line string) {
		f, err := os.OpenFile(filepath.Join(dir, MsiLogFile), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
		if err != nil {
			t.Fatal(err)
		}
		defer f.Close()
		_, _ = f.WriteString(line + "\r\n")
	}
	if err := msiUninstall(context.Background(), true, &fakeSystem{}, tg, suite, appendCreating, msiNow); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(suite, MsiLogFile)); !os.IsNotExist(err) {
		t.Fatalf("msi.log survived a clean removal: %v", err)
	}
	if _, err := os.Stat(filepath.Join(suite, "uninstall-1")); err != nil {
		t.Fatalf("something else in NovieriRMM was removed: %v", err)
	}
}

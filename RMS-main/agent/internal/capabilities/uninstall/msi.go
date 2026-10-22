package uninstall

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// MsiUninstallArg is the one argument main.go routes to MsiMain. The Group
// Policy MSI's removal custom action runs the INSTALLED binary with it, as
// SYSTEM, before Windows Installer deletes the MSI's files
// (docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md §2.3,
// "Uninstall"). It carries nothing else: what to remove is this binary's own
// knowledge, exactly as for the signed agent.uninstall job.
const MsiUninstallArg = "--msi-uninstall"

// MsiLogFile is the MSI's diagnostics in %ProgramData%\ITSuite, the file
// install.MsiLogFile names: one line per run of either custom action.
const MsiLogFile = "msi.log"

// maxMsiLogReason bounds the reason one line carries.
const maxMsiLogReason = 500

// notElevated is --msi-uninstall's refusal, the same as --msi-install's. A
// constant, not an error value: this package is linked into the service, and
// a package-level value made by a call is initialised in every service process.
const notElevated = "--msi-uninstall must run as SYSTEM or an elevated administrator; " +
	"it is the ITMS MSI's removal step"

// msiLogLine is one msi.log line: a UTC timestamp, the mode, and `ok` or
// `failed: <reason>`, on one line -- install.msiLogLine's format. The reason
// is an error this package wrote; none carries a credential.
func msiLogLine(now time.Time, err error) string {
	outcome := "ok"
	if err != nil {
		reason := strings.Join(strings.Fields(err.Error()), " ")
		if len(reason) > maxMsiLogReason {
			reason = reason[:maxMsiLogReason] + "..."
		}
		outcome = "failed: " + reason
	}
	return now.UTC().Format(time.RFC3339) + " " + MsiUninstallArg + " " + outcome
}

// msiUninstall is --msi-uninstall: refused unless elevated, exactly as
// --msi-install is; otherwise RunForMSI. A FAILURE is then logged to msi.log in
// logDir, with its reason. A clean removal logs nothing: it has just deleted
// msi.log on purpose, and the log writer (secdir.AppendLine) creates the file
// whenever %ProgramData%\ITSuite is still there -- which it is whenever the
// directory also holds something that is not the MSI's. windows-latest showed
// that on 2026-09-30 ("msi.log survived msiexec /x").
func msiUninstall(ctx context.Context, elevated bool, sys System, t Targets, logDir string,
	log func(dir, line string), now time.Time) error {
	err := errors.New(notElevated)
	if elevated {
		err = RunForMSI(ctx, sys, t, filepath.Join(logDir, MsiLogFile))
	}
	if err != nil {
		log(logDir, msiLogLine(now, err))
	}
	return err
}

// RunForMSI is agent.uninstall's removal, run in place by an MSI removal: the
// service is stopped and deleted, the credentials and the data directory
// removed (removeServiceAndData). Unlike RunHelper it does not wait -- no job
// asked for this, so there is no answer to get onto the wire first -- and it
// does not remove the binary or its directory: they are the MSI's files, the
// binary is this process's own image, and Windows Installer removes both
// right after this returns.
//
// Then, as RunHelper does, each of t.RemoveIfEmpty goes if nothing else is in
// it: for an MSI, %ProgramData%\ITSuite (msiTargets). Only a removal with no
// failure first deletes logFile, the MSI's msi.log, which the install left
// there; a removal that failed keeps it, and --msi-uninstall adds its reason.
func RunForMSI(ctx context.Context, sys System, t Targets, logFile string) error {
	if err := checkTargets(t); err != nil {
		return err
	}
	var errs []error
	note := func(err error) {
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			errs = append(errs, err)
		}
	}
	removeServiceAndData(ctx, sys, t, note)
	if len(errs) == 0 && logFile != "" {
		note(os.Remove(logFile))
	}
	for _, dir := range t.RemoveIfEmpty {
		_ = os.Remove(dir) // fails, correctly, if anything else is in it
	}
	return errors.Join(errs...)
}

// msiTargets is what an MSI removal removes: the Windows targets for the
// installed binary and the data directory every Windows installer uses,
// %ProgramData%\ITSuite\agent (internal/install/layout), and then
// %ProgramData%\ITSuite itself if it is empty. %ProgramFiles%\ITSuite is not
// among them: it is the MSI's directory, and Windows Installer removes it.
// Pure, so it is tested on any GOOS.
func msiTargets(programData, binary string) Targets {
	t := For("windows", binary, msiLogDir(programData)+`\agent`)
	t.RemoveIfEmpty = []string{msiLogDir(programData)}
	return t
}

// msiLogDir is %ProgramData%\ITSuite, where msi.log lives.
func msiLogDir(programData string) string {
	if programData == "" {
		programData = `C:\ProgramData`
	}
	return strings.TrimRight(programData, `\`) + `\ITSuite`
}

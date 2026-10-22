package install

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"github.com/toeshee/it-suite/agent/internal/install/layout"
)

// THE GROUP POLICY MSI'S INSTALL MODE (docs/superpowers/specs/
// 2026-09-30-gpo-msi-and-subnet-push-design.md §2.3). Windows Installer copies
// its-agent.exe and msi-enroll.json into %ProgramFiles%\ITSuite, then runs
// ONE deferred custom action as SYSTEM, with no impersonation and no desktop:
//
//	"%ProgramFiles%\ITSuite\its-agent.exe" --msi-install
//
// The code is never on that command line: it is in msi-enroll.json, which
// this mode reads, hands to the link installer's own sequence (the token
// file), and deletes. Joining is then the install link's, unchanged: the
// allowed networks, the hourly limit, Waiting for review.
//
// IDLE BUDGET. The service never calls into this: cmd/agent routes
// MsiInstallArg by comparing a constant, and nothing here is a package-level
// value that costs anything to initialise (dormant_test.go).

// MsiInstallArg is the ONLY argument the MSI's install custom action passes.
const MsiInstallArg = "--msi-install"

// MsiSettingsFile is the settings file the MSI places beside the binary.
const MsiSettingsFile = "msi-enroll.json"

// maxMsiSettingsBytes bounds the settings read: two short strings.
const maxMsiSettingsBytes = 4096

// MsiLogFile is the MSI's diagnostics, in %ProgramData%\ITSuite: one line per
// run of --msi-install or --msi-uninstall, for the operator whose Group
// Policy install did not happen (deploy/WINDOWS-PUSH.md §7). The uninstall
// capability writes the same file (uninstall.MsiLogFile).
const MsiLogFile = "msi.log"

// maxMsiLogReason bounds the reason one line carries.
const maxMsiLogReason = 500

// msiLogLine is one msi.log line: a UTC timestamp, the mode, and `ok` or
// `failed: <reason>`, on one line. NEVER the code or the settings file's
// contents: the reason is an error this package wrote, and no error it
// writes carries either (TestMsiLogNeverCarriesTheCodeOrTheSettings).
func msiLogLine(now time.Time, mode string, err error) string {
	outcome := "ok"
	if err != nil {
		reason := strings.Join(strings.Fields(err.Error()), " ")
		if len(reason) > maxMsiLogReason {
			reason = reason[:maxMsiLogReason] + "..."
		}
		outcome = "failed: " + reason
	}
	return now.UTC().Format(time.RFC3339) + " " + mode + " " + outcome
}

// msiLogDir is %ProgramData%\ITSuite: the data directory's parent, which the
// install secures before anything inside it is created.
func msiLogDir(programData string) string {
	d := NewPlan("", programData, "").DataDir
	return d[:strings.LastIndex(d, `\`)]
}

// msiSettings is msi-enroll.json: exactly these two members.
type msiSettings struct {
	Console string `json:"console"`
	Code    string `json:"code"`
}

// parseMsiSettings requires `console` to be a bare https origin
// (layout.CheckEnrollOrigin, the rule every installed service's
// ITS_AGENT_ENROLL_URL follows) and `code` to be 32 URL-safe characters, and
// refuses anything else: another member, a second value, or more than
// maxMsiSettingsBytes. No error it returns contains the code.
func parseMsiSettings(b []byte) (Link, error) {
	if len(b) > maxMsiSettingsBytes {
		return Link{}, fmt.Errorf("%s is larger than %d bytes", MsiSettingsFile, maxMsiSettingsBytes)
	}
	dec := json.NewDecoder(strings.NewReader(string(b)))
	dec.DisallowUnknownFields()
	var s msiSettings
	if err := dec.Decode(&s); err != nil {
		return Link{}, fmt.Errorf("%s is not the MSI's settings file: %w", MsiSettingsFile, err)
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return Link{}, fmt.Errorf("%s holds more than one value", MsiSettingsFile)
	}
	if err := layout.CheckEnrollOrigin(s.Console); err != nil {
		return Link{}, fmt.Errorf("%s: %w", MsiSettingsFile, err)
	}
	if len(s.Code) != codeLength || !validCode(s.Code) {
		return Link{}, fmt.Errorf("%s does not hold an install link code", MsiSettingsFile)
	}
	return Link(s), nil
}

// msiSystem is everything --msi-install does to the machine. The real one is
// msi_windows.go; msi_test.go's fake records every call.
type msiSystem interface {
	// Elevated reports an elevated token: SYSTEM, or an elevated administrator.
	Elevated() bool
	// Enrolled reports an identity in the plan's data directory AND a
	// registered service. An identity alone -- a leftover, or one a standard
	// user planted -- is not enrolled: the full install runs, and its
	// ownership checks refuse a planted directory.
	Enrolled(p Plan) bool
	// EnsureRunning starts the named service if it is stopped.
	EnsureRunning(name string) error
	// ReadSettings reads a regular file of at most limit bytes, never through
	// a link.
	ReadSettings(path string, limit int64) ([]byte, error)
	// Install is the link installer's sequence for a binary already at
	// p.Target: directories (parent first), token file, service, start.
	Install(p Plan, code string) error
	// Remove deletes one file.
	Remove(path string) error
	// Log appends one line to msi.log in dir, creating the file only after
	// dir has been secured. It never fails the install: a line that cannot be
	// written is dropped.
	Log(dir, line string)
}

// runMsiInstall is --msi-install: msiInstall, then its one msi.log line.
func runMsiInstall(sys msiSystem, exe, programFiles, programData string, now time.Time) error {
	err := msiInstall(sys, exe, programFiles, programData)
	sys.Log(msiLogDir(programData), msiLogLine(now, MsiInstallArg, err))
	return err
}

// msiInstall is --msi-install. exe is this binary's own path; programFiles and
// programData are the environment's %ProgramFiles% and %ProgramData%.
func msiInstall(sys msiSystem, exe, programFiles, programData string) error {
	// GPO deployment was the operator's decision, and SYSTEM has no desktop
	// to ask on: no question, only the privilege check.
	if !sys.Elevated() {
		return errors.New("--msi-install must run as SYSTEM or an elevated administrator; " +
			"it is the ITMS MSI's install step")
	}
	p := NewPlan(programFiles, programData, "")
	if !strings.EqualFold(exe, p.Target) {
		return fmt.Errorf("--msi-install sets up the agent the MSI placed at %s, not %s", p.Target, exe)
	}
	settings := p.InstallDir + `\` + MsiSettingsFile
	if sys.Enrolled(p) {
		// Re-assigning the GPO, an upgrade, a reboot: nothing to join. The
		// settings file still goes, and the service the MSI stopped before
		// copying files is started again.
		if err := removeSettings(sys, settings); err != nil {
			return err
		}
		return sys.EnsureRunning(p.ServiceName)
	}
	raw, err := sys.ReadSettings(settings, maxMsiSettingsBytes)
	if err != nil {
		return fmt.Errorf("read %s: %w", settings, err)
	}
	link, err := parseMsiSettings(raw)
	if err != nil {
		return err
	}
	p = NewPlan(programFiles, programData, link.Console)
	if err := sys.Install(p, link.Code); err != nil {
		return err
	}
	return removeSettings(sys, settings)
}

// removeSettings deletes msi-enroll.json. If it cannot, the install fails
// (spec §2.3): Windows Installer rolls back, and the file goes with it.
func removeSettings(sys msiSystem, path string) error {
	if err := sys.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("%s could not be deleted, so the install fails: %w", path, err)
	}
	return nil
}

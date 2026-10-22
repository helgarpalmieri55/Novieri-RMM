// Package layout is the Windows install layout as DATA: the one place that
// names %ProgramFiles%\ITSuite, %ProgramData%\ITSuite\agent, the push staging
// directory, the service name and its recovery actions.
//
// It also carries the SSH push's hand-off constants and the enroll-origin
// check both ends of that push apply (push.go).
//
// It is a LEAF (errors, strings and time only) so that both consumers can share it
// without either linking the other: internal/install (PR 1's self-installer,
// which re-exports Plan/NewPlan) and capabilities/pushinstall (PR 2's WinRM
// push, whose fixed PowerShell is built from the same Plan). Importing
// internal/install from the push capability instead would make every service
// process link the installer, and internal/install's imports_test -- the idle
// budget measured by what is linked -- could no longer tell.
package layout

import (
	"strings"
	"time"
)

// These are agent/packaging/windows/install.ps1's values, copied, and
// internal/install's plan_test.go reads that script to keep them identical.
// ServiceDescription is exported for that test.
const (
	serviceName         = "its-agent"
	displayName         = "ITMS Agent"
	ServiceDescription  = "Toeshee endpoint agent. Maintains one outbound mutual-TLS connection to the control plane and executes only cryptographically signed jobs. Opens no listening port."
	defaultProgramFiles = `C:\Program Files`
	defaultProgramData  = `C:\ProgramData`
)

// Plan is everything the self-installer does, as data (tested on any OS).
type Plan struct {
	InstallDir string
	Target     string
	DataDir    string
	TokenPath  string
	// StagingDir is where a WinRM push downloads and verifies the artefact
	// before installing (design §2.2, plan W1). Under the SAME root as the rest
	// of the layout (D5), so there is one place the vendor directory is named.
	StagingDir  string
	ServiceName string
	DisplayName string
	Description string
	// Environment is the service's `Environment` MultiString: the data
	// directory and the enrollment URL. NEVER the code -- that value is
	// readable by anyone who can read the service key.
	Environment []string
	// RecoveryResetS and RecoveryDelays are `sc.exe failure ... reset= 300
	// actions= restart/5000/restart/15000/restart/60000`.
	RecoveryResetS uint32
	RecoveryDelays []time.Duration
}

// NewPlan builds the plan from %ProgramFiles%, %ProgramData% and the console
// URL the installer's file name gave (FromFileName).
func NewPlan(programFiles, programData, console string) Plan {
	if programFiles == "" {
		programFiles = defaultProgramFiles
	}
	if programData == "" {
		programData = defaultProgramData
	}
	installDir := winJoin(programFiles, "ITSuite")
	dataDir := winJoin(programData, "ITSuite", "agent")
	return Plan{
		InstallDir:     installDir,
		Target:         winJoin(installDir, "its-agent.exe"),
		DataDir:        dataDir,
		TokenPath:      winJoin(dataDir, "enroll-token"),
		StagingDir:     winJoin(programData, "ITSuite", "push"),
		ServiceName:    serviceName,
		DisplayName:    displayName,
		Description:    ServiceDescription,
		Environment:    []string{"ITS_AGENT_DATA_DIR=" + dataDir, "ITS_AGENT_ENROLL_URL=" + console},
		RecoveryResetS: 300,
		RecoveryDelays: []time.Duration{5 * time.Second, 15 * time.Second, 60 * time.Second},
	}
}

// winJoin joins with a backslash on every GOOS, so the plan is testable on Linux.
func winJoin(parts ...string) string {
	out := strings.TrimRight(parts[0], `\`)
	for _, p := range parts[1:] {
		out += `\` + strings.Trim(p, `\`)
	}
	return out
}

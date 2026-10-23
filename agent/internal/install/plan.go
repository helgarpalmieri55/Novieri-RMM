package install

import "github.com/toeshee/it-suite/agent/internal/install/layout"

// Plan is everything the self-installer does, as data (tested on any OS).
//
// The layout itself lives in internal/install/layout, a leaf package, so the
// WinRM push (capabilities/pushinstall) builds its fixed PowerShell from the
// SAME Plan without linking this package into every service process (see
// layout's package comment and imports_test.go). These aliases keep every
// existing caller unchanged.
type Plan = layout.Plan

// NewPlan builds the plan from %ProgramFiles%, %ProgramData% and the console
// URL the installer's file name gave (FromFileName).
func NewPlan(programFiles, programData, console string) Plan {
	return layout.NewPlan(programFiles, programData, console)
}

// serviceDescription is install.ps1's service description (plan_test.go).
const serviceDescription = layout.ServiceDescription

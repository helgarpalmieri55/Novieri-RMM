//go:build windows

package inventory

import (
	"context"
	"testing"
	"time"
)

// TestGatherAgainstTheRealPowerShellScript runs facts_windows.go's gather()
// unmodified: the one PowerShell invocation (windowsFactsScript, a
// compile-time constant -- see facts_windows.go), against the real
// powershell.exe on this runner.
//
// parseWindowsFacts itself already has fixture-driven coverage that runs on
// any GOOS, including linux CI (parse_test.go). What that cannot reach is
// whether powershell.exe is actually on PATH under this argv and whether the
// script's own output is shaped the way parseWindowsFacts expects -- both
// properties of a real Windows machine that no fixture can stand in for.
// Until this job started running on windows-latest, nothing here had ever
// executed on Windows.
func TestGatherAgainstTheRealPowerShellScript(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	f := gather(ctx)

	if f.Hostname == "" {
		t.Error("Hostname is empty; os.Hostname() failed")
	}
	if f.Architecture == "" {
		t.Error("Architecture is empty; runtime.GOARCH should never be")
	}
	if f.OSVersion == "" {
		t.Error("OSVersion is empty; the PowerShell script did not run or its output was not " +
			"in the shape parseWindowsFacts expects")
	}
	t.Logf("real Windows gather(): hostname=%q fqdn=%q os=%q build=%q arch=%q serial=%q cpu=%q ram=%d components=%d",
		f.Hostname, f.FQDN, f.OSVersion, f.OSBuild, f.Architecture, f.Serial, f.CPUModel, f.RAMBytes, len(f.Components))
	// CPUModel, RAMBytes and Serial are not gated: what fields the script
	// populates in what shape is exactly what this test exists to observe
	// for the first time, not to assume in advance.
}

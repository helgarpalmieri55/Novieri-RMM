package install

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestThePlanIsInstallPs1sLayout(t *testing.T) {
	p := NewPlan(`C:\Program Files`, `C:\ProgramData`, "https://itms.example.com")
	want := Plan{
		InstallDir:     `C:\Program Files\NovieriRMM`,
		Target:         `C:\Program Files\NovieriRMM\novieri-agent.exe`,
		DataDir:        `C:\ProgramData\NovieriRMM\agent`,
		TokenPath:      `C:\ProgramData\NovieriRMM\agent\enroll-token`,
		StagingDir:     `C:\ProgramData\NovieriRMM\push`,
		ServiceName:    "novieri-agent",
		DisplayName:    "Novieri RMM Agent",
		Description:    serviceDescription,
		Environment:    []string{`ITS_AGENT_DATA_DIR=C:\ProgramData\NovieriRMM\agent`, "ITS_AGENT_ENROLL_URL=https://itms.example.com"},
		RecoveryResetS: 300,
		RecoveryDelays: []time.Duration{5 * time.Second, 15 * time.Second, 60 * time.Second},
	}
	if !reflect.DeepEqual(p, want) {
		t.Fatalf("NewPlan =\n%+v\nwant\n%+v", p, want)
	}
}

func TestEmptyEnvironmentFallsBackToTheWindowsDefaults(t *testing.T) {
	p := NewPlan("", "", "https://itms.example.com")
	if p.Target != `C:\Program Files\NovieriRMM\novieri-agent.exe` || p.DataDir != `C:\ProgramData\NovieriRMM\agent` {
		t.Fatalf("plan = %+v", p)
	}
}

// THE SELF-INSTALLER AND install.ps1 MUST NOT DRIFT APART. Two installers for
// one service is a second truth about the service; this pins the values the
// self-installer copied against the script they were copied from.
func TestThePlanAgreesWithInstallPs1(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "packaging", "windows", "install.ps1"))
	if err != nil {
		t.Fatal(err)
	}
	script := string(raw)
	p := NewPlan(`C:\Program Files`, `C:\ProgramData`, "https://itms.example.com")
	for _, fragment := range []string{
		`$ServiceName = '` + p.ServiceName + `'`,
		`$DisplayName = '` + p.DisplayName + `'`,
		`$Description = '` + p.Description + `'`,
		`[string]$InstallDir = "$env:ProgramFiles\NovieriRMM"`,
		`[string]$DataDir = "$env:ProgramData\NovieriRMM\agent"`,
		`$tokenFile = Join-Path $DataDir 'enroll-token'`,
		`reset= 300 actions= restart/5000/restart/15000/restart/60000`,
		`'NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators'`,
		`$environment = @("ITS_AGENT_DATA_DIR=$DataDir")`,
		`$environment += "ITS_AGENT_ENROLL_URL=$effectiveUrl"`,
	} {
		if !strings.Contains(script, fragment) {
			t.Errorf("install.ps1 no longer contains %q; update plan.go and this test together", fragment)
		}
	}
}

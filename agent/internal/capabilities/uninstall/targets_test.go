package uninstall

import (
	"path/filepath"
	"reflect"
	"testing"
)

func TestTheCredentialsAreNamedAndAllFourAreThere(t *testing.T) {
	// The same four uninstall.ps1 and darwin/uninstall.sh remove BY NAME before
	// the recursive remove, so a partial failure still takes the private keys.
	want := []string{"enroll-token", "agent.key", "agent.key.pending", "previous.json"}
	for _, goos := range []string{"windows", "linux", "darwin"} {
		if got := For(goos, "b", "d").Credentials; !reflect.DeepEqual(got, want) {
			t.Fatalf("%s credentials = %v, want %v", goos, got, want)
		}
	}
}

func TestEachOSRemovesWhatItsInstallerPlaced(t *testing.T) {
	linux := For("linux", "/usr/local/bin/novieri-agent", "/var/lib/novieri-agent")
	if linux.ServiceName != "novieri-agent" ||
		!reflect.DeepEqual(linux.Remove, []string{"/etc/systemd/system/novieri-agent.service", "/etc/novieri-agent"}) {
		t.Fatalf("linux targets = %+v", linux)
	}
	darwin := For("darwin", "/usr/local/libexec/novieri-agent", "/var/lib/novieri-agent")
	if darwin.ServiceName != "com.novieri.rmm.agent" ||
		!reflect.DeepEqual(darwin.Remove, []string{"/Library/LaunchDaemons/com.novieri.rmm.agent.plist", "/var/log/novieri-agent"}) {
		t.Fatalf("darwin targets = %+v", darwin)
	}
	win := For("windows", `C:\Program Files\NovieriRMM\novieri-agent.exe`, `C:\ProgramData\NovieriRMM\agent`)
	if win.ServiceName != "novieri-agent" || len(win.Remove) != 0 ||
		!reflect.DeepEqual(win.RemoveIfEmpty, []string{`C:\Program Files\NovieriRMM`}) {
		t.Fatalf("windows targets = %+v", win)
	}
}

func TestTheHelperRefusesTargetsThatCouldNotBeAnAgent(t *testing.T) {
	good := t.TempDir()
	cases := map[string]Targets{
		"relative data dir": {DataDir: "data", Binary: filepath.Join(good, "novieri-agent")},
		"root data dir":     {DataDir: string(filepath.Separator), Binary: filepath.Join(good, "novieri-agent")},
		"binary not ours":   {DataDir: good, Binary: filepath.Join(good, "bash")},
	}
	for name, tc := range cases {
		if err := checkTargets(tc); err == nil {
			t.Errorf("%s: accepted %+v", name, tc)
		}
	}
	if err := checkTargets(Targets{DataDir: good, Binary: filepath.Join(good, "novieri-agent")}); err != nil {
		t.Fatalf("refused a real layout: %v", err)
	}
}

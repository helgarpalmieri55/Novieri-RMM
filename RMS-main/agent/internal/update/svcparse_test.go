package update

import (
	"path/filepath"
	"strings"
	"testing"
)

func TestParseSystemdShow(t *testing.T) {
	out := "MainPID=4242\n" +
		"ExecStart={ path=/usr/local/bin/its-agent ; argv[]=/usr/local/bin/its-agent ; ignore_errors=no ; " +
		"start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }\n"
	pid, path, err := parseSystemdShow(out)
	if err != nil || pid != 4242 || path != "/usr/local/bin/its-agent" {
		t.Fatalf("got %d %q %v", pid, path, err)
	}
	if _, _, err := parseSystemdShow("MainPID=0\nExecStart=\n"); err == nil {
		t.Fatal("a unit with no ExecStart parsed")
	}
}

func TestParseLaunchctlPrint(t *testing.T) {
	out := `system/com.toeshee.itsagent = {
	active count = 1
	path = /Library/LaunchDaemons/com.toeshee.itsagent.plist
	state = running

	program = /usr/local/libexec/its-agent
	arguments = {
		/usr/local/libexec/its-agent
	}

	pid = 512
	immediate reason = speculative
}`
	pid, path, err := parseLaunchctlPrint(out)
	if err != nil || pid != 512 || path != "/usr/local/libexec/its-agent" {
		t.Fatalf("got %d %q %v", pid, path, err)
	}

	noProgram := "system/x = {\n\targuments = {\n\t\t/opt/its-agent\n\t\t--flag\n\t}\n\tpid = 9\n}"
	pid, path, err = parseLaunchctlPrint(noProgram)
	if err != nil || pid != 9 || path != "/opt/its-agent" {
		t.Fatalf("arguments fallback: got %d %q %v", pid, path, err)
	}
}

func TestParseWindowsImagePath(t *testing.T) {
	for in, want := range map[string]string{
		`"C:\Program Files\ITSuite\its-agent.exe"`:        `C:\Program Files\ITSuite\its-agent.exe`,
		`"C:\Program Files\ITSuite\its-agent.exe" --flag`: `C:\Program Files\ITSuite\its-agent.exe`,
		`C:\ITSuite\its-agent.exe`:                        `C:\ITSuite\its-agent.exe`,
		`C:\ITSuite\its-agent.exe -x`:                     `C:\ITSuite\its-agent.exe`,
	} {
		if got := parseWindowsImagePath(in); got != want {
			t.Errorf("%q -> %q, want %q", in, got, want)
		}
	}
}

func TestHelperPlistCarriesNoKeepAlive(t *testing.T) {
	// The plist is only ever written on macOS, where the Layout's paths are
	// POSIX. Built from the Layout rather than a literal so the test states
	// what the plist must carry -- the Layout's own state path -- on any OS.
	l := NewLayout(filepath.FromSlash("/var/lib/its-agent"))
	p := string(helperPlist("/var/lib/its-agent/update/prev/its-agent", HelperApply, l))
	for _, want := range []string{
		"<string>com.toeshee.itsagent.update</string>",
		"<string>--update-helper=apply</string>",
		"<string>" + l.StatePath() + "</string>",
		"<key>LaunchOnlyOnce</key>",
	} {
		if !strings.Contains(p, want) {
			t.Errorf("plist lacks %s", want)
		}
	}
	// On a POSIX host that is exactly the path launchd will be handed.
	if filepath.Separator == '/' &&
		!strings.Contains(p, "<string>/var/lib/its-agent/update/state.json</string>") {
		t.Error("plist lacks the POSIX state path /var/lib/its-agent/update/state.json")
	}
	if strings.Contains(p, "KeepAlive") {
		t.Error("the helper job must not be kept alive")
	}
}

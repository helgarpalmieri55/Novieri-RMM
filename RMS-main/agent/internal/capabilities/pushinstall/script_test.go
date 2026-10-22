package pushinstall

import (
	"fmt"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/install/layout"
)

func testRelease() release {
	return release{Version: "1.6.0", SHA256: strings.Repeat("a", 64), URL: "https://console.example/its-agent/1.6.0/windows/amd64?X-Amz-Signature=abc&X-Amz-Expires=300"}
}

// The PowerShell is built from layout.Plan (the SAME layout PR 1's
// self-installer uses) plus signed-param values. It carries NO operator command
// (2a clause (b)): every dynamic value is a path from the plan, a version, a
// SHA-256, the declared arch or the console origin -- never a caller string.
func TestBuildInstallScriptUsesTheSharedLayoutAndNoCommand(t *testing.T) {
	p := layout.NewPlan(`C:\Program Files`, `C:\ProgramData`, "https://console.example")
	rel := testRelease()
	script, err := BuildInstallScript(p, rel, "amd64", "https://console.example")
	if err != nil {
		t.Fatal(err)
	}

	// The install target and data dir are the shared layout, not a spec-literal
	// path, and are resolved on the TARGET from its own %ProgramFiles% and
	// %ProgramData% (review C1 / Minor 7), never a literal C:\.
	for _, want := range []string{
		`$install   = Join-Path $env:ProgramFiles 'ITSuite'`,
		`$target    = Join-Path $env:ProgramFiles 'ITSuite\its-agent.exe'`,
		`$root      = Join-Path $env:ProgramData 'ITSuite'`,
		`$data      = Join-Path $env:ProgramData 'ITSuite\agent'`,
		`$tokenFile = Join-Path $env:ProgramData 'ITSuite\agent\enroll-token'`,
		// The staging dir is W1's D5-consistent path, not "ITS Agent".
		`$staging   = Join-Path $env:ProgramData 'ITSuite\push'`,
		`'its-agent'`, `'ITMS Agent'`,
		// Recovery from the plan, not re-typed.
		`reset= 300 actions= restart/5000/restart/15000/restart/60000`,
		// The service environment: data dir and enroll URL, never the token.
		`"ITS_AGENT_DATA_DIR=$data"`, `"ITS_AGENT_ENROLL_URL=$enrollUrl"`,
		`$enrollUrl = 'https://console.example'`,
	} {
		if !strings.Contains(script, want) {
			t.Errorf("script lacks %s", want)
		}
	}
	if strings.Contains(script, "ITS Agent") {
		t.Error("script uses the old spec-literal 'ITS Agent' path, not the D5 layout")
	}
	if strings.Contains(script, `C:\`) {
		t.Error("script names a literal C: path; the target's own environment decides")
	}
	// The SHA-256 is verified in the script before the binary is placed.
	lower := strings.ToLower(script)
	hashAt := strings.Index(lower, "get-filehash")
	copyAt := strings.Index(lower, "copy-item")
	if !strings.Contains(script, rel.SHA256) || hashAt < 0 || copyAt < 0 || hashAt > copyAt {
		t.Error("script does not verify the artefact SHA-256 before placing it")
	}
	// The arch is checked before anything is downloaded.
	if strings.Index(script, "PROCESSOR_ARCHITECTURE") > strings.Index(script, "Invoke-WebRequest") {
		t.Error("arch check comes after the download")
	}
	// The binary path carries NO argument (the token is in a file).
	if !strings.Contains(script, "-BinaryPathName \"`\"$target`\"\"") {
		t.Error("service binPath is not the quoted target alone")
	}
}

// THE TOKEN NEVER TRAVELS IN THE SCRIPT. The script is powershell.exe's
// -EncodedCommand, i.e. its command line: readable by process listings, Security
// event 4688 and PowerShell script-block logging. install.ps1 refuses -Token for
// exactly that reason; the push reads it from stdin instead.
func TestTheEnrollTokenIsNotInTheScript(t *testing.T) {
	p := layout.NewPlan("", "", "https://console.example")
	script, err := BuildInstallScript(p, testRelease(), "amd64", "https://console.example")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(script, "[Console]::In.ReadLine()") {
		t.Fatal("the script does not read the token from stdin")
	}
	if strings.Contains(script, "SECRET") || strings.Contains(script, "enroll_token") {
		t.Fatal("token material in the script")
	}
}

func TestBuildInstallScriptRefusesUnsafeValues(t *testing.T) {
	p := layout.NewPlan("", "", "https://console.example")
	good := testRelease()
	cases := map[string]func() (release, string, string){
		"quote in URL": func() (release, string, string) { r := good; r.URL = "https://x/a'b"; return r, "amd64", "https://x" },
		"smart quote in URL": func() (release, string, string) {
			r := good
			r.URL = "https://x/a\u2019b"
			return r, "amd64", "https://x"
		},
		"backtick in URL": func() (release, string, string) { r := good; r.URL = "https://x/a`b"; return r, "amd64", "https://x" },
		"space in URL":    func() (release, string, string) { r := good; r.URL = "https://x/a b"; return r, "amd64", "https://x" },
		"http URL":        func() (release, string, string) { r := good; r.URL = "http://x/a"; return r, "amd64", "https://x" },
		"short digest":    func() (release, string, string) { r := good; r.SHA256 = "abc"; return r, "amd64", "https://x" },
		"non-hex digest": func() (release, string, string) {
			r := good
			r.SHA256 = strings.Repeat("z", 64)
			return r, "amd64", "https://x"
		},
		"unknown arch":          func() (release, string, string) { return good, "x86", "https://x" },
		"console with path":     func() (release, string, string) { return good, "amd64", "https://x/evil" },
		"console with quote":    func() (release, string, string) { return good, "amd64", "https://x'" },
		"console not https":     func() (release, string, string) { return good, "amd64", "http://x" },
		"console with userinfo": func() (release, string, string) { return good, "amd64", "https://u@x" },
	}
	for name, mk := range cases {
		r, arch, console := mk()
		if _, err := BuildInstallScript(p, r, arch, console); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestPSQuoteDoublesEverySingleQuoteForm(t *testing.T) {
	for in, want := range map[string]string{
		`a'b`:         `'a''b'`,
		"a\u2018b":    "'a\u2018\u2018b'",
		"a\u2019b":    "'a\u2019\u2019b'",
		"a\u201ab":    "'a\u201a\u201ab'",
		"a\u201bb":    "'a\u201b\u201bb'",
		`C:\x $y "z"`: `'C:\x $y "z"'`,
	} {
		if got := psQuote(in); got != want {
			t.Errorf("psQuote(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestConsoleOrigin(t *testing.T) {
	for in, want := range map[string]string{
		"https://itms.example.com/its-agent/x?sig=1":      "https://itms.example.com",
		"https://itms.example.com:8443/its-agent/x?sig=1": "https://itms.example.com:8443",
	} {
		got, err := consoleOrigin(in)
		if err != nil || got != want {
			t.Errorf("consoleOrigin(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	for _, bad := range []string{"http://x/y", "https:///y", "https://u:p@x/y", "not a url", ""} {
		if _, err := consoleOrigin(bad); err == nil {
			t.Errorf("consoleOrigin(%q) accepted", bad)
		}
	}
}

// REVIEW C1. A standard user who pre-created %ProgramData%\ITSuite owned it,
// and could swap push\its-agent.exe between the hash and the copy, or the
// data directory, and have their binary run as SYSTEM. These assertions are
// about ORDER in a fixed script, so they are made on the script text; the
// agent-winrm job and the operator's one-PC trial run it for real.
func TestTheScriptTakesTheVendorRootBeforeUsingAnythingUnderIt(t *testing.T) {
	script, err := BuildInstallScript(layout.NewPlan("", "", "https://console.example"), testRelease(), "amd64", "https://console.example")
	if err != nil {
		t.Fatal(err)
	}
	at := func(s string) int {
		i := strings.Index(script, s)
		if i < 0 {
			t.Fatalf("script lacks %q", s)
		}
		return i
	}
	// The root is protected -- created with its descriptor, or taken
	// (takeown /A: owner BUILTIN\Administrators) and re-described -- before
	// the staging or data directory is touched.
	protectRoot := at("Protect-Root $root")
	for _, later := range []string{"New-ProtectedDirectory $staging", "New-ProtectedDirectory $data", "Invoke-WebRequest"} {
		if at(later) < protectRoot {
			t.Errorf("%s runs before the vendor root is protected", later)
		}
	}
	for _, want := range []string{
		// Owner BA and a protected SYSTEM + Administrators DACL, by SID.
		"$sec.SetAccessRuleProtection($true, $false)",
		"'S-1-5-18', 'S-1-5-32-544'",
		"$sec.SetOwner((New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')))",
		"& takeown.exe /F $Path /A",
		// Created WITH its descriptor, never created and then described.
		"[System.IO.Directory]::CreateDirectory($Path, (New-DirSecurity))",
		// Reparse points are refused on all three.
		"[System.IO.FileAttributes]::ReparsePoint",
	} {
		at(want)
	}
	// A root someone else owns is refused, not taken over (the install
	// link's rule): what they planted inside, or a handle they opened
	// earlier, survives a new owner and a new DACL. The refusal comes before
	// takeown, and the discard-and-continue path is gone.
	if at(fmt.Sprintf("if (-not $ours) { exit %d }", scriptExitForeignRoot)) > at("& takeown.exe /F $Path /A") {
		t.Error("a foreign-owned root is taken over before it is refused")
	}
	if strings.Contains(script, "Reset-Or-Protect") || strings.Contains(script, "rootWasOurs") {
		t.Error("the script still discards and re-creates under a root it did not own")
	}
	for _, dir := range []string{"$root", "$staging", "$data"} {
		if !strings.Contains(script, "Assert-SafeDirectory "+dir) {
			t.Errorf("%s is never checked for a reparse point", dir)
		}
	}
	// No PowerShell recursive delete: Windows PowerShell 5.1's Remove-Item
	// -Recurse can descend through a junction. Directory.Delete does not.
	if strings.Contains(script, "-Recurse") {
		t.Error("the script uses Remove-Item -Recurse")
	}
	// Every recursive delete goes through Remove-Tree, which checks its
	// target for a reparse point first.
	if strings.Count(script, "[System.IO.Directory]::Delete(") != 1 {
		t.Error("a recursive delete bypasses Remove-Tree")
	}
	if !strings.Contains(script, "function Remove-Tree([string]$Path) {\n    Assert-SafeDirectory $Path\n") {
		t.Error("Remove-Tree does not check for a reparse point first")
	}
}

func TestTheInstalledBinaryIsRehashedBeforeTheServiceStarts(t *testing.T) {
	// REVIEW C1, the most important half: whatever happened in the staging
	// directory, the file in admin-only %ProgramFiles% is hashed again and a
	// mismatch is refused before Start-Service.
	script, err := BuildInstallScript(layout.NewPlan("", "", "https://console.example"), testRelease(), "amd64", "https://console.example")
	if err != nil {
		t.Fatal(err)
	}
	copyAt := strings.Index(script, "Copy-Item -LiteralPath $pkg -Destination $target")
	rehashAt := strings.Index(script, "(Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash")
	startAt := strings.Index(script, "Start-Service -Name $svc")
	envAt := strings.Index(script, "New-ItemProperty -Path $serviceKey")
	if copyAt < 0 || rehashAt < 0 || startAt < 0 || !(copyAt < rehashAt && rehashAt < startAt && rehashAt < envAt) {
		t.Fatalf("copy %d, re-hash %d, service environment %d, start %d: the placed binary is not re-verified before use",
			copyAt, rehashAt, envAt, startAt)
	}
	if !strings.Contains(script, "exit "+strconvItoa(scriptExitInstalledMismatch)) {
		t.Error("a re-hash mismatch has no exit code of its own")
	}
}

func strconvItoa(n int) string { return fmt.Sprint(n) }

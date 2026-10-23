//go:build e2e && windows

package e2e

import (
	"crypto/ed25519"
	"crypto/rand"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
	"unicode/utf16"
)

// TestMsiInstallWindows is the Windows row of the GPO MSI spec's testing
// section (docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md):
// the MSI is built by the server's own builder (backend/app/services/
// msi_build.py) with a real wixl (MSYS2's msitools), installed by msiexec as
// SYSTEM from a scheduled task -- what Group Policy does at computer startup
// -- and the agent joins through the install link. A second MSI of the same
// version is an upgrade that changes nothing; `msiexec /x` removes it all.
//
// NOT covered, and cannot be here: real Group Policy assignment at startup.
// The operator's one-OU trial is that (deploy/WINDOWS-PUSH.md §7).
func TestMsiInstallWindows(t *testing.T) {
	binDir := os.Getenv("E2E_BIN_DIR")
	if binDir == "" {
		t.Fatal("E2E_BIN_DIR is unset; run internal/update/e2e/build.sh first")
	}
	msitools := os.Getenv("E2E_MSITOOLS_DIR")
	if msitools == "" {
		t.Fatal("E2E_MSITOOLS_DIR is unset: the directory holding MSYS2's wixl.exe, msibuild.exe and msiinfo.exe")
	}
	python := os.Getenv("E2E_PYTHON")
	if python == "" {
		python = "python"
	}
	repoRoot, err := filepath.Abs(filepath.Join("..", "..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	if v := os.Getenv("E2E_REPO_ROOT"); v != "" {
		repoRoot = v
	}
	const (
		code = "e2eMSIcode_0123456789-ABCDEFGHIJ"
		org  = "5b0f9d8e-7a6c-4b3d-9e2f-1a0b9c8d7e6f"
	)

	jobPub, jobKey, _ := ed25519.GenerateKey(rand.Reader)
	p := newPKI(t)
	plat := newPlatform()
	caPath := filepath.Join(t.TempDir(), "e2e-ca.pem")
	if err := os.WriteFile(caPath, p.caPEM, 0o644); err != nil {
		t.Fatal(err)
	}
	plat.trustCA(t, caPath)
	t.Cleanup(func() { plat.untrustCA(t) })
	gw := startGateway(t, p, jobKey)
	enroll := startEnroll(t, p, gw.url, jobPub)
	t.Cleanup(func() { plat.uninstall(t, repoRoot) })

	work := t.TempDir()
	agentExe := filepath.Join(work, "its-agent.exe")
	copyFileT(t, filepath.Join(binDir, "A"+exe()), agentExe)
	codeFile := filepath.Join(work, "code")
	if err := os.WriteFile(codeFile, []byte(code), 0o600); err != nil {
		t.Fatal(err)
	}
	build := func(name string) string {
		t.Helper()
		out := filepath.Join(work, name)
		cmd := exec.Command(python, filepath.Join(repoRoot, "backend", "app", "services", "msi_build.py"),
			"--exe", agentExe, "--console", enroll.url, "--code-file", codeFile, "--org", org,
			"--version", versionA, "--arch", "amd64", "--out", out)
		cmd.Env = append(os.Environ(), "PATH="+msitools+string(os.PathListSeparator)+os.Getenv("PATH"))
		b, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("msi_build.py: %v\n%s", err, b)
		}
		if strings.Contains(string(b), code) {
			t.Fatalf("the builder printed the code:\n%s", b)
		}
		return out
	}
	settings := filepath.Join(filepath.Dir(plat.target()), "msi-enroll.json")

	// INSTALL, as Group Policy does it: msiexec as SYSTEM, no UI.
	first := build("ITMS-Agent-first-amd64.msi")
	if rc, log := msiexecAsSystem(t, `/i "`+first+`"`, filepath.Join(work, "install.log")); rc != 0 {
		t.Fatalf("msiexec /i exited %d\n%s", rc, log)
	} else if strings.Contains(log, code) {
		t.Fatal("the msiexec log carries the code")
	}
	if n := productCount(t); n != 1 {
		t.Fatalf("%d ITMS Agent products registered after one install, want 1", n)
	}
	if _, err := os.Stat(settings); !os.IsNotExist(err) {
		t.Fatalf("msi-enroll.json survived the install: %v", err)
	}
	pathName := ps(t, "(Get-CimInstance Win32_Service -Filter \"Name='its-agent'\").PathName")
	if strings.TrimSpace(pathName) != `"`+plat.target()+`"` {
		t.Fatalf("service command line = %q; it must be the quoted path and nothing else", pathName)
	}
	env := ps(t, "(Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\its-agent').Environment -join '|'")
	if !strings.Contains(env, "ITS_AGENT_ENROLL_URL="+enroll.url) || strings.Contains(env, code) {
		t.Fatalf("service environment = %q", env)
	}
	assertOwnedSDDL(t, "data directory", plat.dataDir())
	assertOwnedSDDL(t, "data directory's parent", filepath.Dir(plat.dataDir()))
	// DIAGNOSTICS: one line per run in %ProgramData%\ITSuite\msi.log, and
	// never the code or the settings.
	suite := filepath.Dir(plat.dataDir())
	msiLog := filepath.Join(suite, "msi.log")
	assertMsiLog(t, msiLog, code, enroll.url, 1)

	// JOINED THROUGH THE LINK: one enrollment, carrying link_code and no token.
	gw.waitVersion(versionA, 3*time.Minute)
	bodies := enroll.requests()
	if len(bodies) != 1 || bodies[0]["link_code"] != code {
		t.Fatalf("enrollment requests = %v; want one carrying link_code", bodies)
	}
	if _, hasToken := bodies[0]["token"]; hasToken {
		t.Fatal("the enrollment carried a token member as well as the link code")
	}
	if _, err := os.Stat(filepath.Join(plat.dataDir(), "enroll-token")); !os.IsNotExist(err) {
		t.Fatalf("the token file survived enrollment: %v", err)
	}
	lines := ps(t, "Get-CimInstance Win32_Process -Filter \"Name='its-agent.exe'\" | ForEach-Object { $_.CommandLine }")
	if strings.Contains(lines, code) {
		t.Fatalf("a running agent carries the code in its command line: %s", lines)
	}

	// A SECOND MSI, same version, new ProductCode: a major upgrade on an
	// enrolled machine. Nothing is re-enrolled, the identity is untouched, the
	// service the MSI stopped runs again, and there is still one product.
	identity, err := os.ReadFile(filepath.Join(plat.dataDir(), "identity.json"))
	if err != nil {
		t.Fatal(err)
	}
	second := build("ITMS-Agent-second-amd64.msi")
	if rc, log := msiexecAsSystem(t, `/i "`+second+`"`, filepath.Join(work, "upgrade.log")); rc != 0 {
		t.Fatalf("msiexec /i (upgrade) exited %d\n%s", rc, log)
	}
	if n := productCount(t); n != 1 {
		t.Fatalf("%d ITMS Agent products registered after an upgrade, want 1", n)
	}
	again, err := os.ReadFile(filepath.Join(plat.dataDir(), "identity.json"))
	if err != nil || string(again) != string(identity) {
		t.Fatalf("the upgrade changed the identity (err %v)", err)
	}
	if _, err := os.Stat(settings); !os.IsNotExist(err) {
		t.Fatalf("msi-enroll.json survived the upgrade: %v", err)
	}
	waitServiceRunning(t, 2*time.Minute)
	assertMsiLog(t, msiLog, code, enroll.url, 2)
	if n := len(enroll.requests()); n != 1 {
		t.Fatalf("the upgrade enrolled again: %d enrollment requests", n)
	}

	// REMOVE: msiexec /x runs --msi-uninstall, then the files go.
	if rc, log := msiexecAsSystem(t, `/x "`+second+`"`, filepath.Join(work, "remove.log")); rc != 0 {
		t.Fatalf("msiexec /x exited %d\n%s", rc, log)
	}
	if svc := strings.TrimSpace(ps(t, "(Get-Service -Name its-agent -ErrorAction SilentlyContinue).Name")); svc != "" {
		t.Fatalf("the service survived msiexec /x: %q", svc)
	}
	for _, path := range []string{plat.target(), plat.dataDir(), msiLog} {
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Fatalf("%s survived msiexec /x: %v", path, err)
		}
	}
	// %ProgramData%\ITSuite goes too, unless something that is not the MSI's
	// is in it (another test's leftovers on the runner, say).
	if entries, err := os.ReadDir(suite); err == nil {
		if len(entries) == 0 {
			t.Fatalf("%s survived msiexec /x empty", suite)
		}
		for _, e := range entries {
			t.Logf("%s kept after msiexec /x: it also holds %s", suite, e.Name())
		}
	} else if !os.IsNotExist(err) {
		t.Fatalf("read %s after msiexec /x: %v", suite, err)
	}
	if n := productCount(t); n != 0 {
		t.Fatalf("%d ITMS Agent products registered after removal, want 0", n)
	}
}

// msiexecAsSystem runs `msiexec <args> /qn /norestart /l*v <log>` as SYSTEM
// through a scheduled task and returns its exit code and the log.
func msiexecAsSystem(t *testing.T, args, logPath string) (int, string) {
	t.Helper()
	const task = "ITMS-MSI-E2E"
	script := fmt.Sprintf(`$ErrorActionPreference = 'Stop'
$a = New-ScheduledTaskAction -Execute 'msiexec.exe' -Argument '%s /qn /norestart /l*v "%s"'
Register-ScheduledTask -TaskName '%s' -Action $a -User 'NT AUTHORITY\SYSTEM' -RunLevel Highest -Force | Out-Null
$start = (Get-Date).AddSeconds(-2)
Start-ScheduledTask -TaskName '%s'
$deadline = (Get-Date).AddMinutes(10)
do {
  Start-Sleep -Seconds 2
  $state = (Get-ScheduledTask -TaskName '%s').State
  $info = Get-ScheduledTaskInfo -TaskName '%s'
} while (($state -eq 'Running' -or $info.LastRunTime -lt $start) -and (Get-Date) -lt $deadline)
Unregister-ScheduledTask -TaskName '%s' -Confirm:$false
$info.LastTaskResult`, args, logPath, task, task, task, task, task)
	out := strings.Fields(ps(t, script))
	if len(out) == 0 {
		t.Fatal("the scheduled task reported no result")
	}
	rc, err := strconv.Atoi(out[len(out)-1])
	if err != nil {
		t.Fatalf("the scheduled task's result %q is not a number", out[len(out)-1])
	}
	raw, _ := os.ReadFile(logPath)
	return rc, decodeLog(raw)
}

// decodeLog reads an msiexec log: UTF-16LE with a BOM on current Windows,
// otherwise single-byte text.
func decodeLog(b []byte) string {
	if len(b) >= 2 && b[0] == 0xFF && b[1] == 0xFE {
		u := make([]uint16, 0, len(b)/2)
		for i := 2; i+1 < len(b); i += 2 {
			u = append(u, uint16(b[i])|uint16(b[i+1])<<8)
		}
		return string(utf16.Decode(u))
	}
	return string(b)
}

// assertMsiLog reads msi.log: at least `installs` lines saying
// `--msi-install ok`, one line each, never the code or the console address.
func assertMsiLog(t *testing.T, path, code, console string, installs int) {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("msi.log: %v", err)
	}
	log := string(b)
	if strings.Contains(log, code) || strings.Contains(log, console) {
		t.Fatalf("msi.log carries the code or the settings:\n%s", log)
	}
	if n := strings.Count(log, " --msi-install ok\r\n"); n < installs {
		t.Fatalf("msi.log has %d --msi-install ok lines, want %d:\n%s", n, installs, log)
	}
}

// productCount is how many installed products are named "ITMS Agent".
func productCount(t *testing.T) int {
	t.Helper()
	out := strings.TrimSpace(ps(t, "@(Get-ChildItem 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall' | "+
		"Get-ItemProperty -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -eq 'ITMS Agent' }).Count"))
	n, err := strconv.Atoi(out)
	if err != nil {
		t.Fatalf("product count %q", out)
	}
	return n
}

func waitServiceRunning(t *testing.T, d time.Duration) {
	t.Helper()
	deadline := time.Now().Add(d)
	for {
		status := strings.TrimSpace(ps(t, "(Get-Service -Name its-agent -ErrorAction SilentlyContinue).Status"))
		if status == "Running" {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("the its-agent service is %q, not Running, %s after the upgrade", status, d)
		}
		time.Sleep(2 * time.Second)
	}
}

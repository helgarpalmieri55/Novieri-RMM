//go:build e2e && windows

package e2e

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type windowsPlatform struct{}

func newPlatform() platform { return windowsPlatform{} }

func (windowsPlatform) dataDir() string {
	return filepath.Join(os.Getenv("ProgramData"), "ITSuite", "agent")
}

func (windowsPlatform) target() string {
	return filepath.Join(os.Getenv("ProgramFiles"), "ITSuite", "its-agent.exe")
}

func ps(t *testing.T, script string) string {
	return run(t, "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script)
}

func psQuiet(script string) (string, error) {
	return runQuiet("powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script)
}

func (windowsPlatform) install(t *testing.T, repoRoot, binary string) {
	run(t, "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
		"-File", filepath.Join(repoRoot, `agent\packaging\windows\install.ps1`), "-Binary", binary)
}

func (windowsPlatform) uninstall(t *testing.T, repoRoot string) {
	_, _ = runQuiet("powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
		"-File", filepath.Join(repoRoot, `agent\packaging\windows\uninstall.ps1`))
}

var windowsCAThumb string

func (windowsPlatform) trustCA(t *testing.T, pemPath string) {
	cer := pemPath + ".cer"
	b, err := os.ReadFile(pemPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cer, b, 0o644); err != nil {
		t.Fatal(err)
	}
	out := ps(t, "(Import-Certificate -FilePath '"+cer+"' -CertStoreLocation Cert:\\LocalMachine\\Root).Thumbprint")
	windowsCAThumb = strings.TrimSpace(out)
}

func (windowsPlatform) untrustCA(*testing.T) {
	if windowsCAThumb != "" {
		_, _ = psQuiet("Remove-Item -Path Cert:\\LocalMachine\\Root\\" + windowsCAThumb)
	}
}

func (windowsPlatform) configuredBinary(t *testing.T) string {
	out := ps(t, "(Get-CimInstance Win32_Service -Filter \"Name='its-agent'\").PathName")
	return strings.Trim(strings.TrimSpace(out), `"`)
}

const helperQuery = "Get-CimInstance Win32_Process -Filter \"Name='its-agent.exe'\" | " +
	"Where-Object { $_.CommandLine -like '*--update-helper*' }"

func (windowsPlatform) killHelper(t *testing.T) {
	ps(t, helperQuery+" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
}

func (windowsPlatform) helperGone() (bool, string) {
	out, err := psQuiet(helperQuery + " | ForEach-Object { $_.ProcessId }")
	if err != nil {
		return false, err.Error()
	}
	return strings.TrimSpace(out) == "", strings.TrimSpace(out)
}

func (windowsPlatform) listeners() ([]string, error) {
	out, err := psQuiet("Get-NetTCPConnection -State Listen | ForEach-Object { " +
		"$p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue; " +
		"\"$($p.ProcessName) $($_.LocalAddress):$($_.LocalPort)\" }; " +
		"Get-NetUDPEndpoint | ForEach-Object { " +
		"$p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue; " +
		"\"$($p.ProcessName) udp $($_.LocalAddress):$($_.LocalPort)\" }")
	if err != nil {
		return nil, err
	}
	return agentLines(out), nil
}

//go:build windows

package install

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"

	"github.com/toeshee/it-suite/agent/internal/secdir"
)

// RunMsi is `its-agent.exe --msi-install` (msi.go). It returns the process
// exit code: anything but 0 fails the MSI, which rolls back.
func RunMsi(exe string) int {
	// FIRST, before anything can load a library (review I2).
	if err := windows.SetDefaultDllDirectories(windows.LOAD_LIBRARY_SEARCH_SYSTEM32); err != nil {
		fmt.Fprintln(os.Stderr, "its-agent --msi-install: cannot restrict the DLL search path:", err)
		return 1
	}
	if err := runMsiInstall(realMsi{}, exe, os.Getenv("ProgramFiles"), os.Getenv("ProgramData"), time.Now()); err != nil {
		fmt.Fprintln(os.Stderr, "its-agent --msi-install:", err)
		return 1
	}
	fmt.Fprintln(os.Stderr, "its-agent --msi-install: installed; the service joins through the install link")
	return 0
}

// identityFile is config's identity file name: its presence, with the
// service registered, is what "already enrolled" means.
const identityFile = "identity.json"

// realMsi is msiSystem on Windows.
type realMsi struct{}

func (realMsi) Elevated() bool { return windows.GetCurrentProcessToken().IsElevated() }

func (realMsi) Enrolled(p Plan) bool {
	if info, err := os.Stat(filepath.Join(p.DataDir, identityFile)); err != nil || !info.Mode().IsRegular() {
		return false
	}
	m, err := mgr.Connect()
	if err != nil {
		return false
	}
	defer m.Disconnect()
	s, err := m.OpenService(p.ServiceName)
	if err != nil {
		return false
	}
	s.Close()
	return true
}

func (realMsi) EnsureRunning(name string) error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("open the Service Control Manager: %w", err)
	}
	defer m.Disconnect()
	s, err := m.OpenService(name)
	if err != nil {
		return fmt.Errorf("open the %s service: %w", name, err)
	}
	defer s.Close()
	st, err := s.Query()
	if err != nil {
		return fmt.Errorf("query the %s service: %w", name, err)
	}
	if st.State != svc.Stopped {
		return nil
	}
	if err := s.Start(); err != nil && !errors.Is(err, windows.ERROR_SERVICE_ALREADY_RUNNING) {
		return fmt.Errorf("start the %s service: %w", name, err)
	}
	return nil
}

func (realMsi) ReadSettings(path string, limit int64) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("%s is not a regular file", path)
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > limit {
		return nil, fmt.Errorf("%s is larger than %d bytes", path, limit)
	}
	return b, nil
}

func (realMsi) Install(p Plan, code string) error { return applyInPlace(p, code) }

func (realMsi) Remove(path string) error { return os.Remove(path) }

// Log appends to %ProgramData%\ITSuite\msi.log. secdir.AppendLine secures
// the directory first (creating it, as the install would), refuses one that
// anyone but Administrators or SYSTEM owns, and never follows a link. A line
// it cannot write is dropped: diagnostics never fail an install.
func (realMsi) Log(dir, line string) { _ = secdir.AppendLine(dir, MsiLogFile, line, true) }

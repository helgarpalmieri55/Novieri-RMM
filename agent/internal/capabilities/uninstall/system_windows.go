//go:build windows

package uninstall

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"

	"github.com/toeshee/it-suite/agent/internal/config"
	"github.com/toeshee/it-suite/agent/internal/secdir"
)

// Supported: this build advertises agent.uninstall.
const Supported = true

// hardenProcess restricts DLL resolution to System32 (review I2). It is the
// first statement of HelperMain: the helper runs from a copy in a directory a
// planted DLL must not be able to reach.
func hardenProcess() error {
	return windows.SetDefaultDllDirectories(windows.LOAD_LIBRARY_SEARCH_SYSTEM32)
}

// LaunchHelper copies the binary into a staging directory of its own,
// `%ProgramData%\ITSuite\uninstall-<n>` (review M1), and starts the COPY
// detached: a running image cannot be deleted, and the helper must delete the
// installed one. The directory is CREATED owned by Administrators with the
// protected SYSTEM+Administrators DACL (secdir.MkdirOwned, one CreateDirectory
// call, so there is no moment it exists with a wider ACL), and the copy is
// created O_EXCL inside it. The service's temp directory is not used: it is
// shared, and a helper image a local user could replace between the copy and
// the start would run as SYSTEM.
//
// ITSuite ITSELF is secured first (secdir.SecureOwned: owner Administrators,
// protected DACL, a junction refused). The staging directory's own descriptor
// is not enough: whoever owns ITSuite holds FILE_DELETE_CHILD on it, and could
// rename `uninstall-<n>` away between the copy and the start and put a
// directory holding their own its-agent-uninstall.exe in its place. A
// standard user can pre-create ITSuite on a default install (Users may create
// folders in C:\ProgramData), and an endpoint installed before the installers
// secured it may still have one they own.
//
// The SCM does not kill a service's children on stop; breakaway is retried
// without if the job object denies it (the self-update helper's pattern,
// internal/update/service_windows.go).
func LaunchHelper(_ context.Context, binary, dataDir string) error {
	dir, copyPath := helperStaging(os.Getenv("ProgramData"), time.Now().UnixNano())
	if err := secdir.SecureOwned(filepath.Dir(dir), nil); err != nil {
		return fmt.Errorf("secure %s: %w", filepath.Dir(dir), err)
	}
	if err := secdir.MkdirOwned(dir); err != nil {
		return fmt.Errorf("create the helper's staging directory: %w", err)
	}
	if err := copyFile(binary, copyPath); err != nil {
		return fmt.Errorf("copy the helper: %w", err)
	}
	start := func(flags uint32) error {
		cmd := exec.Command(copyPath, HelperArg)
		cmd.Env = append(os.Environ(), config.EnvDataDir+"="+dataDir, EnvBinary+"="+binary)
		cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: flags, HideWindow: true}
		if err := cmd.Start(); err != nil {
			return err
		}
		return cmd.Process.Release()
	}
	base := uint32(windows.DETACHED_PROCESS | windows.CREATE_NEW_PROCESS_GROUP)
	if err := start(base | windows.CREATE_BREAKAWAY_FROM_JOB); err == nil {
		return nil
	}
	return start(base)
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o700)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

type platformSystem struct{}

func open(name string) (*mgr.Mgr, *mgr.Service, error) {
	m, err := mgr.Connect()
	if err != nil {
		return nil, nil, err
	}
	s, err := m.OpenService(name)
	if err != nil {
		m.Disconnect()
		return nil, nil, err
	}
	return m, s, nil
}

func (platformSystem) StopService(_ context.Context, name string) error {
	m, s, err := open(name)
	if err != nil {
		return err
	}
	defer m.Disconnect()
	defer s.Close()
	if _, err := s.Control(svc.Stop); err != nil && !errors.Is(err, windows.ERROR_SERVICE_NOT_ACTIVE) {
		return err
	}
	deadline := time.Now().Add(60 * time.Second)
	for time.Now().Before(deadline) {
		st, err := s.Query()
		if err != nil {
			return err
		}
		if st.State == svc.Stopped {
			return nil
		}
		time.Sleep(500 * time.Millisecond)
	}
	return fmt.Errorf("the %s service did not stop within 60s", name)
}

func (platformSystem) DeleteService(_ context.Context, name string) error {
	m, s, err := open(name)
	if err != nil {
		return err
	}
	defer m.Disconnect()
	defer s.Close()
	return s.Delete()
}

func (platformSystem) PurgePackage(context.Context) (bool, error) { return false, nil }

// ScheduleDelete asks Windows to delete the helper's own copy at the next
// boot, and then its staging directory (bootDeletions: file first, so the
// directory is empty when its turn comes).
func (platformSystem) ScheduleDelete(path string) error {
	var errs []error
	for _, target := range bootDeletions(path) {
		p, err := windows.UTF16PtrFromString(target)
		if err != nil {
			errs = append(errs, err)
			continue
		}
		if err := windows.MoveFileEx(p, nil, windows.MOVEFILE_DELAY_UNTIL_REBOOT); err != nil {
			errs = append(errs, fmt.Errorf("schedule %s for deletion at boot: %w", target, err))
		}
	}
	return errors.Join(errs...)
}

func (platformSystem) HelperExiting(context.Context) {}

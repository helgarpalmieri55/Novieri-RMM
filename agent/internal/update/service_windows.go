//go:build windows

package update

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"syscall"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

// SystemServices drives the Service Control Manager.
type SystemServices struct{}

func openService() (*mgr.Mgr, *mgr.Service, error) {
	m, err := mgr.Connect()
	if err != nil {
		return nil, nil, err
	}
	s, err := m.OpenService(windowsService)
	if err != nil {
		m.Disconnect()
		return nil, nil, err
	}
	return m, s, nil
}

func (SystemServices) Identify(context.Context) (string, error) {
	m, s, err := openService()
	if err != nil {
		return "", fail(ReasonNotRunningAsService, err)
	}
	defer m.Disconnect()
	defer s.Close()
	cfg, err := s.Config()
	if err != nil {
		return "", fail(ReasonNotRunningAsService, err)
	}
	st, err := s.Query()
	if err != nil {
		return "", fail(ReasonNotRunningAsService, err)
	}
	return checkIdentity(int(st.ProcessId), parseWindowsImagePath(cfg.BinaryPathName), true)
}

// LaunchHelper starts a detached process; the SCM does not kill a service's
// children on stop. Breakaway is retried without if the job denies it.
func (SystemServices) LaunchHelper(_ context.Context, prev, phase string, l Layout, _ string) error {
	start := func(flags uint32) error {
		cmd := exec.Command(prev, "--update-helper="+phase, "--state", l.StatePath())
		cmd.Env = append(os.Environ(), "ITS_AGENT_DATA_DIR="+l.DataDir())
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

func (SystemServices) StopForSwap() bool { return true }

func (SystemServices) Stop(context.Context) error {
	m, s, err := openService()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	defer s.Close()
	st, err := s.Query()
	if err != nil {
		return err
	}
	if st.State != svc.Stopped {
		if _, err := s.Control(svc.Stop); err != nil && !errors.Is(err, windows.ERROR_SERVICE_NOT_ACTIVE) {
			return err
		}
	}
	deadline := time.Now().Add(60 * time.Second)
	for time.Now().Before(deadline) {
		st, err := s.Query()
		if err != nil {
			return err
		}
		if st.State == svc.Stopped {
			// The image stays mapped briefly after the service reports stopped.
			time.Sleep(time.Second)
			return nil
		}
		time.Sleep(500 * time.Millisecond)
	}
	return fmt.Errorf("the %s service did not stop within 60s", windowsService)
}

func (SystemServices) Start(context.Context) error {
	m, s, err := openService()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	defer s.Close()
	if err := s.Start(); err != nil && !errors.Is(err, windows.ERROR_SERVICE_ALREADY_RUNNING) {
		return err
	}
	return nil
}

func (w SystemServices) Restart(ctx context.Context) error {
	if err := w.Stop(ctx); err != nil {
		return err
	}
	return w.Start(ctx)
}

func (SystemServices) HelperExiting(context.Context) {}

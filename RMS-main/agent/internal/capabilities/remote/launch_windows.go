package remote

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"sync"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Supported reports whether this build can share a screen. It decides whether
// remote.screen.open is registered, and so advertised.
const Supported = true

// DefaultLauncher starts the helper in the active console session as the
// logged-on user (spec §2.4).
func DefaultLauncher() Launcher {
	exe, _ := os.Executable()
	return consoleLauncher{exe: exe}
}

type consoleLauncher struct{ exe string }

// Launch implements Launcher: WTSGetActiveConsoleSessionId, WTSQueryUserToken,
// DuplicateTokenEx, CreateEnvironmentBlock, CreateProcessAsUser on
// winsta0\default. Only the service (LocalSystem) holds the privilege
// WTSQueryUserToken needs.
func (l consoleLauncher) Launch(ctx context.Context) (Helper, error) {
	sid := windows.WTSGetActiveConsoleSessionId()
	if sid == 0xFFFFFFFF {
		return nil, ErrNoUserSession
	}
	var user windows.Token
	if err := windows.WTSQueryUserToken(sid, &user); err != nil {
		if errors.Is(err, windows.ERROR_NO_TOKEN) {
			return nil, ErrNoUserSession
		}
		return nil, fmt.Errorf("WTSQueryUserToken: %w", err)
	}
	defer user.Close()
	var primary windows.Token
	if err := windows.DuplicateTokenEx(user, windows.MAXIMUM_ALLOWED, nil, windows.SecurityIdentification,
		windows.TokenPrimary, &primary); err != nil {
		return nil, fmt.Errorf("DuplicateTokenEx: %w", err)
	}
	defer primary.Close()
	var env *uint16
	if err := windows.CreateEnvironmentBlock(&env, primary, false); err != nil {
		return nil, fmt.Errorf("CreateEnvironmentBlock: %w", err)
	}
	defer windows.DestroyEnvironmentBlock(env)
	return startHelper(primary, l.exe, []string{HelperArg}, env)
}

// startHelper creates the helper suspended, with exactly its three pipe ends
// inheritable (PROC_THREAD_ATTRIBUTE_HANDLE_LIST), puts it in a kill-on-close
// job so it cannot outlive the service, then lets it run. A zero token runs
// it as the calling user (tests).
func startHelper(token windows.Token, exe string, args []string, env *uint16) (h *winHelper, err error) {
	sa := &windows.SecurityAttributes{Length: uint32(unsafe.Sizeof(windows.SecurityAttributes{})), InheritHandle: 1}
	var childIn, parentIn, parentOut, childOut, parentErr, childErr windows.Handle
	closeAll := func(hs ...*windows.Handle) {
		for _, x := range hs {
			if *x != 0 {
				_ = windows.CloseHandle(*x)
				*x = 0
			}
		}
	}
	defer func() {
		closeAll(&childIn, &childOut, &childErr)
		if err != nil {
			closeAll(&parentIn, &parentOut, &parentErr)
		}
	}()
	for _, p := range [][2]*windows.Handle{{&childIn, &parentIn}, {&parentOut, &childOut}, {&parentErr, &childErr}} {
		if err := windows.CreatePipe(p[0], p[1], sa, 0); err != nil {
			return nil, fmt.Errorf("CreatePipe: %w", err)
		}
	}
	for _, x := range []windows.Handle{parentIn, parentOut, parentErr} {
		if err := windows.SetHandleInformation(x, windows.HANDLE_FLAG_INHERIT, 0); err != nil {
			return nil, err
		}
	}

	attrs, err := windows.NewProcThreadAttributeList(1)
	if err != nil {
		return nil, err
	}
	defer attrs.Delete()
	inherit := []windows.Handle{childIn, childOut, childErr}
	if err := attrs.Update(windows.PROC_THREAD_ATTRIBUTE_HANDLE_LIST, unsafe.Pointer(&inherit[0]),
		uintptr(len(inherit))*unsafe.Sizeof(inherit[0])); err != nil {
		return nil, err
	}
	si := windows.StartupInfoEx{
		StartupInfo: windows.StartupInfo{
			Cb:        uint32(unsafe.Sizeof(windows.StartupInfoEx{})),
			Flags:     windows.STARTF_USESTDHANDLES,
			StdInput:  childIn,
			StdOutput: childOut,
			StdErr:    childErr,
			Desktop:   wide(`winsta0\default`),
		},
		ProcThreadAttributeList: attrs.List(),
	}
	cmd := syscall.EscapeArg(exe)
	for _, a := range args {
		cmd += " " + syscall.EscapeArg(a)
	}
	flags := uint32(windows.CREATE_NO_WINDOW | windows.CREATE_UNICODE_ENVIRONMENT |
		windows.EXTENDED_STARTUPINFO_PRESENT | windows.CREATE_SUSPENDED)
	var pi windows.ProcessInformation
	if token != 0 {
		err = windows.CreateProcessAsUser(token, wide(exe), wide(cmd), nil, nil, true, flags, env, nil, &si.StartupInfo, &pi)
	} else {
		err = windows.CreateProcess(wide(exe), wide(cmd), nil, nil, true, flags, env, nil, &si.StartupInfo, &pi)
	}
	if err != nil {
		return nil, fmt.Errorf("create helper: %w", err)
	}
	defer windows.CloseHandle(pi.Thread)

	job, err := killOnCloseJob()
	if err == nil {
		err = windows.AssignProcessToJobObject(job, pi.Process)
	}
	if err != nil {
		_ = windows.TerminateProcess(pi.Process, 1)
		_ = windows.CloseHandle(pi.Process)
		if job != 0 {
			_ = windows.CloseHandle(job)
		}
		return nil, fmt.Errorf("helper job: %w", err)
	}
	if _, err := windows.ResumeThread(pi.Thread); err != nil {
		_ = windows.TerminateProcess(pi.Process, 1)
		_ = windows.CloseHandle(pi.Process)
		_ = windows.CloseHandle(job)
		return nil, fmt.Errorf("ResumeThread: %w", err)
	}

	h = &winHelper{
		process: pi.Process, job: job, pid: int(pi.ProcessId),
		in:     os.NewFile(uintptr(parentIn), "helper-stdin"),
		out:    os.NewFile(uintptr(parentOut), "helper-stdout"),
		exited: make(chan struct{}),
	}
	errFile := os.NewFile(uintptr(parentErr), "helper-stderr")
	parentIn, parentOut, parentErr = 0, 0, 0
	h.wg.Add(2)
	go func() {
		defer h.wg.Done()
		_, _ = windows.WaitForSingleObject(h.process, windows.INFINITE)
		close(h.exited)
	}()
	go func() {
		defer h.wg.Done()
		_, _ = io.Copy(&h.stderr, errFile)
		_ = errFile.Close()
	}()
	return h, nil
}

func killOnCloseJob() (windows.Handle, error) {
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return 0, err
	}
	info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
	info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
		windows.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
	if _, err := windows.SetInformationJobObject(job, windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info))); err != nil {
		_ = windows.CloseHandle(job)
		return 0, err
	}
	return job, nil
}

func wide(s string) *uint16 {
	p, _ := windows.UTF16PtrFromString(s)
	return p
}

type winHelper struct {
	process, job windows.Handle
	pid          int
	in, out      *os.File
	exited       chan struct{}
	stderr       capped
	wg           sync.WaitGroup
	once         sync.Once
}

func (h *winHelper) Stdin() io.WriteCloser   { return h.in }
func (h *winHelper) Stdout() io.Reader       { return h.out }
func (h *winHelper) Exited() <-chan struct{} { return h.exited }
func (h *winHelper) PID() int                { return h.pid }
func (h *winHelper) Kill() error             { return windows.TerminateProcess(h.process, 1) }
func (h *winHelper) Stderr() string          { return h.stderr.String() }

// Close releases every handle once the process is gone. Closing the job
// kills anything still in it.
func (h *winHelper) Close() error {
	h.once.Do(func() {
		_ = h.in.Close()
		_ = h.out.Close()
		_ = windows.CloseHandle(h.job)
		<-h.exited
		h.wg.Wait()
		_ = windows.CloseHandle(h.process)
	})
	return nil
}

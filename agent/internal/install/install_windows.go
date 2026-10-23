//go:build windows

package install

import (
	"context"
	"crypto/ed25519"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"

	"github.com/toeshee/it-suite/agent/internal/secdir"
	"github.com/toeshee/it-suite/agent/internal/update"
)

const (
	windowTitle    = "ITMS agent"
	msgInstalled   = "Installed — this computer will appear in ITMS within a minute."
	msgCancelled   = "The installation was cancelled. It needs an administrator to approve the Windows prompt."
	msgNotElevated = "Windows did not give the installer administrator rights. Open it again and approve the prompt."
	msgDeclined    = "Nothing was installed."
	msgNotRelease  = "This file could not be confirmed as the ITMS agent the install link serves, so nothing " +
		"was installed. Download it again from the install link. Details: "
)

// MessageBox answers (winuser.h; golang.org/x/sys does not name them).
const (
	idOK  = 1
	idYes = 6
	idNo  = 7
)

// The two Win32 calls that show UI or start a process, as variables so
// install_windows_test.go can observe them. Function values, not calls:
// dormant_test.go still sees nothing to initialise.
var (
	shellExecute = windows.ShellExecute
	messageBox   = windows.MessageBox
)

// The directory operations prepareDirs performs, as variables so
// dirs_windows_test.go can observe their ORDER. Function values, as above.
var (
	secureOwned = secdir.SecureOwned
	checkOwned  = secdir.CheckOwned
	mkdirAll    = os.MkdirAll
)

// Run is the self-installer. It returns the process exit code. `link` is
// FromFileName(exe): the elevated re-launch reads it again from its own file
// name, so neither the code nor the console crosses the UAC boundary.
func Run(args []string, exe string, link Link) int {
	// FIRST, in both phases, before anything can load a library (review I2):
	// DLLs resolve from System32 only, never from the folder this file was
	// opened from -- Downloads, a share, a USB stick. Fails closed.
	if err := windows.SetDefaultDllDirectories(windows.LOAD_LIBRARY_SEARCH_SYSTEM32); err != nil {
		fmt.Fprintln(os.Stderr, "The installer could not restrict where Windows loads its libraries from, "+
			"so nothing was installed:", err)
		return 1
	}
	ui := os.Getenv(EnvNoUI) != "1"
	if code, done := prelude(args, exe, link, ui, windows.GetCurrentProcessToken().IsElevated()); done {
		return code
	}
	return install(exe, link, ui)
}

// prelude is everything before the elevated work. done=false means "this
// process is elevated and the person has agreed: install".
//
// The host question (review I3) is asked in the process the person started --
// no arguments -- whether or not it is already elevated, and never again in
// the UAC re-launch, which exists only because they said Yes. `ui` false
// (EnvNoUI) skips it only when already elevated (asksHost, final review M6);
// it always suppresses the result window.
func prelude(args []string, exe string, link Link, ui, elevated bool) (code int, done bool) {
	relaunched := len(args) == 1 && args[0] == ElevatedArg
	if asksHost(relaunched, !ui, elevated) && !confirmHost(link) {
		fmt.Fprintln(os.Stderr, msgDeclined)
		return 1, true
	}
	if elevated {
		return 0, false
	}
	if relaunched {
		return report(ui, false, msgNotElevated), true
	}
	if err := relaunchElevated(exe); err != nil {
		if errors.Is(err, windows.ERROR_CANCELLED) {
			return report(ui, false, msgCancelled), true
		}
		return report(ui, false, "The installer could not ask Windows for administrator rights: "+err.Error()), true
	}
	return 0, true // the elevated copy installs and reports
}

// confirmHost asks the Yes/No question and reports a Yes.
func confirmHost(link Link) bool {
	text, _ := windows.UTF16PtrFromString(confirmText(link))
	title, _ := windows.UTF16PtrFromString(windowTitle)
	answer, err := messageBox(0, text, title,
		windows.MB_YESNO|windows.MB_ICONQUESTION|windows.MB_DEFBUTTON2|windows.MB_SETFOREGROUND|windows.MB_TOPMOST)
	return err == nil && answer == idYes
}

// install is the elevated phase.
func install(exe string, link Link, ui bool) int {
	// Review I1. The file was opened from a place a non-administrator can
	// write. Hold it open, readable and NOT writable, renamable or deletable by
	// anyone else, for the rest of this process -- and copy from THIS handle,
	// never by path again.
	src, err := openShareReadOnly(exe)
	if err != nil {
		return report(ui, false, "The installer could not open itself: "+err.Error())
	}
	defer src.Close()

	// The digest of the bytes this handle holds -- the bytes stage() copies,
	// since no one else can write them while it is open -- names the release
	// to the console (final review I1): a copy saved before a newer release
	// was promoted still installs. The staged copy is checked again below.
	digest, err := sha256Hex(src)
	if err != nil {
		return report(ui, false, "The installer could not read itself: "+err.Error())
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*metaTimeout)
	defer cancel()
	meta, err := fetchReleaseMeta(ctx, metaClient(nil), link, runtime.GOARCH, digest)
	if err != nil {
		return report(ui, false, msgNotRelease+err.Error())
	}
	pub, err := update.ReleaseKey()
	if err != nil {
		return report(ui, false, msgNotRelease+err.Error())
	}
	plan := NewPlan(os.Getenv("ProgramFiles"), os.Getenv("ProgramData"), link.Console)
	if err := apply(plan, src, meta, pub, link.Code); err != nil {
		var nr notRelease
		if errors.As(err, &nr) {
			return report(ui, false, msgNotRelease+nr.Error())
		}
		return report(ui, false, "The agent could not be installed: "+err.Error())
	}
	return report(ui, true, msgInstalled)
}

// openShareReadOnly opens path for reading with FILE_SHARE_READ ONLY: while
// the handle is open, no other process can open the file for writing, rename
// it or delete it.
func openShareReadOnly(path string) (*os.File, error) {
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return nil, err
	}
	h, err := windows.CreateFile(p, windows.GENERIC_READ, windows.FILE_SHARE_READ, nil,
		windows.OPEN_EXISTING, windows.FILE_ATTRIBUTE_NORMAL, 0)
	if err != nil {
		return nil, err
	}
	return os.NewFile(uintptr(h), path), nil
}

// relaunchElevated starts this same file with UAC's "runas" verb and ONE
// parameter, ElevatedArg. The code is not in it.
func relaunchElevated(exe string) error {
	verb, err := windows.UTF16PtrFromString("runas")
	if err != nil {
		return err
	}
	file, err := windows.UTF16PtrFromString(exe)
	if err != nil {
		return err
	}
	params, err := windows.UTF16PtrFromString(ElevatedArg)
	if err != nil {
		return err
	}
	return shellExecute(0, verb, file, params, nil, windows.SW_SHOWNORMAL)
}

// notRelease marks a staged file that failed verifyStaged.
type notRelease struct{ error }

// apply does what install.ps1 does, with the staged binary VERIFIED before
// anything that is not trivially undone: directories and their owner and ACL
// (the data directory's parent first, prepareDirs), the binary staged from
// the held handle and checked (review I1), the token file, stop an existing
// service, the binary moved into place, the service, its environment, its
// recovery actions, start.
func apply(p Plan, src *os.File, meta ReleaseMeta, pub ed25519.PublicKey, code string) error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("open the Service Control Manager: %w", err)
	}
	defer m.Disconnect()
	existing, err := m.OpenService(p.ServiceName) // nil when there is none
	if err == nil {
		defer existing.Close()
	}

	if err := prepareDirs(p, existing == nil); err != nil {
		return err
	}

	staged := filepath.Join(p.InstallDir, ".its-agent.exe.new")
	if err := stage(src, staged); err != nil {
		return err
	}
	if err := verifyFile(staged, meta, pub); err != nil {
		_ = os.Remove(staged)
		return notRelease{err}
	}

	if err := writeToken(p, code); err != nil {
		return err
	}
	if existing != nil {
		if err := stop(existing); err != nil {
			return err
		}
	}
	if err := renameWithRetry(staged, p.Target); err != nil {
		return err
	}
	return registerAndStart(m, existing, p)
}

// applyInPlace is apply for a binary that is ALREADY at p.Target, placed there
// by the Group Policy MSI (GPO MSI spec 2026-09-30 §2.3): the same
// directories (parent first), token file, service, environment, recovery
// actions and start -- without staging a copy or asking the console to vouch
// for it. The MSI wraps only bytes whose SHA-256 and release signature the
// server checked (app/modules/agent_distribution/msi.py), and a binary
// checking its own bytes would prove nothing.
func applyInPlace(p Plan, code string) error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("open the Service Control Manager: %w", err)
	}
	defer m.Disconnect()
	existing, err := m.OpenService(p.ServiceName) // nil when there is none
	if err == nil {
		defer existing.Close()
	}
	if err := prepareDirs(p, existing == nil); err != nil {
		return err
	}
	if err := writeToken(p, code); err != nil {
		return err
	}
	if existing != nil {
		if err := stop(existing); err != nil {
			return err
		}
	}
	return registerAndStart(m, existing, p)
}

// writeToken creates the token file holding the code. Never written INTO an
// existing file (review I4): a pre-created enroll-token keeps its creator's
// owner and ACL. Removed, then created O_EXCL, inheriting the data
// directory's DACL. No BOM, no newline.
func writeToken(p Plan, code string) error {
	if err := os.Remove(p.TokenPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("remove a previous token file: %w", err)
	}
	tf, err := os.OpenFile(p.TokenPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return fmt.Errorf("create the token file: %w", err)
	}
	if _, err := tf.WriteString(code); err != nil {
		tf.Close()
		return fmt.Errorf("write the token file: %w", err)
	}
	if err := tf.Close(); err != nil {
		return fmt.Errorf("write the token file: %w", err)
	}
	return nil
}

// registerAndStart creates the service when there is none (existing nil),
// then sets its environment and recovery actions and starts it.
func registerAndStart(m *mgr.Mgr, existing *mgr.Service, p Plan) error {
	s := existing
	if s == nil {
		// No arguments on the service command line: CreateService quotes the
		// path and appends nothing.
		// ErrorControl is New-Service's default, which install.ps1 relies on;
		// mgr.Config's zero value would be SERVICE_ERROR_IGNORE instead.
		created, err := m.CreateService(p.ServiceName, p.Target, mgr.Config{
			StartType:    mgr.StartAutomatic,
			ErrorControl: mgr.ErrorNormal,
			DisplayName:  p.DisplayName,
			Description:  p.Description,
		})
		if err != nil {
			return fmt.Errorf("register the service: %w", err)
		}
		defer created.Close()
		s = created
	}

	key, err := registry.OpenKey(registry.LOCAL_MACHINE,
		`SYSTEM\CurrentControlSet\Services\`+p.ServiceName, registry.SET_VALUE)
	if err != nil {
		return fmt.Errorf("open the service key: %w", err)
	}
	err = key.SetStringsValue("Environment", p.Environment)
	key.Close()
	if err != nil {
		return fmt.Errorf("write the service environment: %w", err)
	}

	actions := make([]mgr.RecoveryAction, 0, len(p.RecoveryDelays))
	for _, d := range p.RecoveryDelays {
		actions = append(actions, mgr.RecoveryAction{Type: mgr.ServiceRestart, Delay: d})
	}
	if err := s.SetRecoveryActions(actions, p.RecoveryResetS); err != nil {
		return fmt.Errorf("set the service recovery actions: %w", err)
	}
	if err := s.Start(); err != nil && !errors.Is(err, windows.ERROR_SERVICE_ALREADY_RUNNING) {
		return fmt.Errorf("start the service: %w", err)
	}
	return nil
}

// prepareDirs creates or secures the three directories, PARENT FIRST.
//
// `%ProgramData%\ITSuite` is secured before `agent` is created or touched
// (2026-09-29). Protecting `agent` alone protected nothing: on a default
// Windows install BUILTIN\Users may create folders in C:\ProgramData and
// CREATOR OWNER gets Full Control on them, so a standard user could
// `mkdir C:\ProgramData\ITSuite` before the install and own it -- and an
// owner of a directory holds FILE_DELETE_CHILD on it. After the install they
// renamed the protected `agent` away and put their own in its place, holding
// an identity.json, pins.json and agent.key that point the SYSTEM service at
// their gateway and their job-signing key; the same control let them race the
// token file. secdir.SecureOwned creates ITSuite with its descriptor, or
// takes ownership of an existing one through a handle that did not follow a
// junction, and refuses a junction or symbolic link outright.
//
// On a FRESH install (no service yet) an existing directory must also belong
// to Administrators, SYSTEM or the administrator running this -- checked on
// the same handle, before anything is changed. Taking ownership does not
// remove what its previous owner planted inside, which is the whole attack.
// A re-install's directories are the previous install's, and are secured
// without that check.
//
// `%ProgramFiles%\ITSuite` is checked (no junction; owner on a fresh install)
// but keeps the ACL it inherits: C:\Program Files grants BUILTIN\Users read
// and execute only, so they cannot pre-create it, and install.ps1 installs
// the binary under the same inherited ACL.
func prepareDirs(p Plan, fresh bool) error {
	accept := func(dir string) func(*windows.SID) error {
		if !fresh {
			return nil
		}
		return func(owner *windows.SID) error { return ownerAllowed(dir, owner) }
	}
	if err := checkOwned(p.InstallDir, accept(p.InstallDir)); err != nil {
		return err
	}
	if err := mkdirAll(p.InstallDir, 0o755); err != nil {
		return err
	}
	parent := filepath.Dir(p.DataDir)
	if err := secureOwned(parent, accept(parent)); err != nil {
		return err
	}
	// install.ps1's protected DACL -- SYSTEM and Administrators, inherited by
	// everything inside -- and the OWNER (review I4): an owner keeps WRITE_DAC
	// whatever the DACL says.
	return secureOwned(p.DataDir, accept(p.DataDir))
}

// ownerAllowed refuses an existing directory owned by anyone but
// Administrators, SYSTEM or this process's own (elevated) user.
func ownerAllowed(dir string, owner *windows.SID) error {
	if owner == nil {
		return fmt.Errorf("%s has no readable owner", dir)
	}
	for _, known := range []windows.WELL_KNOWN_SID_TYPE{windows.WinBuiltinAdministratorsSid, windows.WinLocalSystemSid} {
		if sid, err := windows.CreateWellKnownSid(known); err == nil && owner.Equals(sid) {
			return nil
		}
	}
	if u, err := windows.GetCurrentProcessToken().GetTokenUser(); err == nil && owner.Equals(u.User.Sid) {
		return nil
	}
	name := owner.String()
	if account, domain, _, err := owner.LookupAccount(""); err == nil {
		name = domain + `\` + account
	}
	return fmt.Errorf("%s already exists and belongs to %s, not to an administrator; "+
		"delete it and run the installer again", dir, name)
}

// stage copies the held source into a fresh staging file next to the target
// (created O_EXCL after removing any leftover, in the administrators-only
// install directory).
func stage(src *os.File, staged string) error {
	if _, err := src.Seek(0, io.SeekStart); err != nil {
		return err
	}
	if err := os.Remove(staged); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("remove a leftover %s: %w", staged, err)
	}
	out, err := os.OpenFile(staged, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, src); err != nil {
		out.Close()
		os.Remove(staged)
		return err
	}
	if err := out.Close(); err != nil {
		os.Remove(staged)
		return err
	}
	return nil
}

// verifyFile reads the STAGED bytes back and checks them (verifyStaged).
func verifyFile(staged string, meta ReleaseMeta, pub ed25519.PublicKey) error {
	f, err := os.Open(staged)
	if err != nil {
		return err
	}
	defer f.Close()
	return verifyStaged(pub, meta, runtime.GOARCH, f)
}

func stop(s *mgr.Service) error {
	st, err := s.Query()
	if err != nil || st.State == svc.Stopped {
		return err
	}
	if _, err := s.Control(svc.Stop); err != nil && !errors.Is(err, windows.ERROR_SERVICE_NOT_ACTIVE) {
		return fmt.Errorf("stop the existing service: %w", err)
	}
	for deadline := time.Now().Add(30 * time.Second); time.Now().Before(deadline); time.Sleep(500 * time.Millisecond) {
		if st, err := s.Query(); err == nil && st.State == svc.Stopped {
			return nil
		}
	}
	return errors.New("the existing its-agent service did not stop within 30 seconds")
}

// renameWithRetry: the SCM can report Stopped a moment before the old process
// has released its image (install.ps1 retries 20 times, 500 ms apart).
func renameWithRetry(staged, dst string) error {
	var err error
	for attempt := 0; attempt < 20; attempt++ {
		if err = os.Rename(staged, dst); err == nil {
			return nil
		}
		time.Sleep(500 * time.Millisecond)
	}
	_ = os.Remove(staged)
	return fmt.Errorf("could not write %s -- the previous its-agent.exe is still in use: %w", dst, err)
}

// report shows the result window (or, with EnvNoUI=1, only the stderr line)
// and returns the exit code.
func report(ui, ok bool, message string) int {
	fmt.Fprintln(os.Stderr, message)
	if ui {
		icon := uint32(windows.MB_ICONINFORMATION)
		if !ok {
			icon = windows.MB_ICONERROR
		}
		text, _ := windows.UTF16PtrFromString(message)
		title, _ := windows.UTF16PtrFromString(windowTitle)
		_, _ = messageBox(0, text, title, windows.MB_OK|icon|windows.MB_SETFOREGROUND|windows.MB_TOPMOST)
	}
	if ok {
		return 0
	}
	return 1
}

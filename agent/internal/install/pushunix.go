package install

import (
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/toeshee/it-suite/agent/internal/install/layout"
)

// THE LINUX AND macOS INSTALL MODE, as a plan over a system seam. The SSH push
// (capabilities/pushinstall) places this platform's own published binary and a
// 0600 token file in layout.UnixPushStagingDir and runs ONE fixed command
// against the placed binary:
//
//	/opt/toeshee/agent.pkg install --token-file /opt/toeshee/enroll.token --enroll-url 'https://<console>'
//
// That is invariant 2a(b)'s shape: the only thing that executes on the target
// is the published artefact, and what it does is compiled into it -- the job
// carries values only. What it does is what the shipped
// agent/packaging/{linux,darwin}/install.sh does, in the same order, to the
// same layout (UnixLayoutFor; pushunix_test.go reads both scripts), so there
// is one Linux layout and one macOS layout however the agent arrived.
//
// IDLE BUDGET. Nothing here runs unless cmd/agent's first argument is
// PushArg; nothing here is a package-level value that costs anything to
// initialise (dormant_test.go), and imports_test.go pins what it links.

// PushArg is the install mode's argument (layout.PushSubcommand).
const PushArg = layout.PushSubcommand

// maxTokenBytes bounds the staged token read (config's maxTokenFile).
const maxTokenBytes = 4096

// UnixLayout is agent/packaging/{linux,darwin}/install.sh's layout, as data.
type UnixLayout struct {
	GOOS    string
	BinDir  string
	Binary  string
	DataDir string
	// TokenPath is config.EnrollTokenPath: <DataDir>/enroll-token.
	TokenPath string
	// ConfDir and EnvFile are Linux's: the unit's EnvironmentFile carries
	// ITS_AGENT_ENROLL_URL. Empty on macOS, where the plist carries it.
	ConfDir string
	EnvFile string
	// LogDir is macOS's: the plist's StandardErrorPath directory.
	LogDir      string
	ServiceDir  string
	ServiceFile string
	// Service is the systemd unit name or the launchd label.
	Service string
}

// UnixLayoutFor is the layout for goos, or an error for a system with no
// install mode.
func UnixLayoutFor(goos string) (UnixLayout, error) {
	const dataDir = "/var/lib/its-agent"
	switch goos {
	case "linux":
		return UnixLayout{
			GOOS:        "linux",
			BinDir:      "/usr/local/bin",
			Binary:      "/usr/local/bin/its-agent",
			DataDir:     dataDir,
			TokenPath:   dataDir + "/enroll-token",
			ConfDir:     "/etc/its-agent",
			EnvFile:     "/etc/its-agent/agent.env",
			ServiceDir:  "/etc/systemd/system",
			ServiceFile: "/etc/systemd/system/its-agent.service",
			Service:     "its-agent",
		}, nil
	case "darwin":
		return UnixLayout{
			GOOS:        "darwin",
			BinDir:      "/usr/local/libexec",
			Binary:      "/usr/local/libexec/its-agent",
			DataDir:     dataDir,
			TokenPath:   dataDir + "/enroll-token",
			LogDir:      "/var/log/its-agent",
			ServiceDir:  "/Library/LaunchDaemons",
			ServiceFile: "/Library/LaunchDaemons/com.toeshee.itsagent.plist",
			Service:     "com.toeshee.itsagent",
		}, nil
	}
	return UnixLayout{}, fmt.Errorf("there is no install mode for %q; it runs on Linux and macOS", goos)
}

// stagingDirInfo is what the install mode must know about the directory the
// push staged into, from an lstat: a symlink is reported as one, never as its
// target.
type stagingDirInfo struct {
	IsDir     bool
	IsSymlink bool
	UID       int
	Mode      os.FileMode
}

// unixSystem is everything the install mode does to the machine. The real one
// (push_unix.go) is os and os/exec; the tests' records every call.
type unixSystem interface {
	Geteuid() int
	// LstatDir describes path without following a symlink.
	LstatDir(path string) (stagingDirInfo, error)
	// EvalSymlinks is filepath.EvalSymlinks.
	EvalSymlinks(path string) (string, error)
	// ReadStaged reads a regular file (never through a symlink) of at most
	// limit bytes.
	ReadStaged(path string, limit int64) ([]byte, error)
	// EnsureDir creates path with mode if it is missing. rootOwned also sets
	// the mode and root ownership on a directory that already exists, as
	// install.sh's `install -d -m <mode> -o root -g root` does.
	EnsureDir(path string, mode os.FileMode, rootOwned bool) error
	// WriteFile replaces path atomically with a root-owned file of mode.
	WriteFile(path string, data []byte, mode os.FileMode) error
	// WriteExclusive removes whatever is at path and creates it with O_EXCL
	// and no symlink following, root-owned, mode, before writing a byte.
	WriteExclusive(path string, data []byte, mode os.FileMode) error
	// CopyFile replaces dst atomically with a root-owned copy of src.
	CopyFile(src, dst string, mode os.FileMode) error
	Remove(path string) error
	Run(name string, args ...string) error
}

// pushRequest is the parsed command line.
type pushRequest struct {
	tokenFile string
	enrollURL string
}

// parsePushArgs reads `--token-file <path> --enroll-url <origin>`, each once,
// and nothing else. `--token` is refused as install.sh refuses it: a token in
// argv is readable through /proc/<pid>/cmdline and `ps` by every user.
func parsePushArgs(args []string, l UnixLayout) (pushRequest, error) {
	var req pushRequest
	seen := map[string]bool{}
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if arg == "--token" || strings.HasPrefix(arg, "--token=") {
			return pushRequest{}, errors.New("the enrollment token must not be passed as an argument: argv is " +
				"world-readable through /proc/<pid>/cmdline; pass " + layout.FlagTokenFile + " <0600 file>")
		}
		if arg != layout.FlagTokenFile && arg != layout.FlagEnrollURL {
			return pushRequest{}, fmt.Errorf("unknown argument %q (expected %s <path> %s <https origin>)",
				arg, layout.FlagTokenFile, layout.FlagEnrollURL)
		}
		if seen[arg] {
			return pushRequest{}, fmt.Errorf("%s given twice", arg)
		}
		seen[arg] = true
		if i+1 >= len(args) {
			return pushRequest{}, fmt.Errorf("%s needs a value", arg)
		}
		i++
		if arg == layout.FlagTokenFile {
			req.tokenFile = args[i]
		} else {
			req.enrollURL = args[i]
		}
	}
	if req.tokenFile == "" {
		return pushRequest{}, errors.New("no enrollment token: " + layout.FlagTokenFile + " <path> is required")
	}
	if !strings.HasPrefix(req.tokenFile, "/") {
		return pushRequest{}, errors.New(layout.FlagTokenFile + " must be an absolute path")
	}
	if req.tokenFile == l.TokenPath {
		return pushRequest{}, fmt.Errorf("%s is where the token is installed, not where it is staged", l.TokenPath)
	}
	if err := layout.CheckEnrollOrigin(req.enrollURL); err != nil {
		return pushRequest{}, fmt.Errorf("%s: %w", layout.FlagEnrollURL, err)
	}
	return req, nil
}

// pushInstall is the install mode. exe is the running binary: the file that
// is installed, and deleted afterwards when it is the push's staged copy.
func pushInstall(sys unixSystem, l UnixLayout, args []string, exe string) (err error) {
	req, err := parsePushArgs(args, l)
	if err != nil {
		return err
	}
	// THE STAGED FILES GO WHATEVER HAPPENS NEXT. The token is a single-use
	// credential; a copy left in the staging directory after a failure would
	// outlive the attempt that needed it. The staged binary is removed only
	// when it IS the push's staged copy -- never the installed binary, never a
	// file somebody ran from elsewhere.
	tokenRemoved := false
	untouchable := false
	defer func() {
		if untouchable {
			return
		}
		if !tokenRemoved {
			if rerr := sys.Remove(req.tokenFile); rerr != nil && !errors.Is(rerr, os.ErrNotExist) && err == nil {
				err = fmt.Errorf("remove the staged token %s: %w", req.tokenFile, rerr)
			}
		}
		if isStagedBinary(sys, exe) {
			if rerr := sys.Remove(exe); rerr != nil && !errors.Is(rerr, os.ErrNotExist) && err == nil {
				err = fmt.Errorf("remove the staged binary %s: %w", exe, rerr)
			}
		}
	}()
	if sys.Geteuid() != 0 {
		return errors.New("the install mode must run as root: the push account must log in as root " +
			"(uid 0), as install.sh requires")
	}
	// REVIEW I3. The directory the token and binary were staged in must be a
	// real directory, owned by root, with no group or world access -- the
	// pushing agent makes it fresh (`rm -rf` + `mkdir -m 0700`), so anything
	// else means someone else could have written, swapped or linked what is
	// about to be read and installed. Refused before a byte is read, and
	// nothing in it is touched afterwards, not even to clean up.
	stagingDir := parentDir(req.tokenFile)
	info, err := sys.LstatDir(stagingDir)
	if err != nil || info.IsSymlink || !info.IsDir || info.UID != 0 || info.Mode.Perm()&0o077 != 0 {
		untouchable = true
		if err != nil {
			return fmt.Errorf("the staging directory %s: %w", stagingDir, err)
		}
		return fmt.Errorf("the staging directory %s is not a root-owned directory that only root "+
			"can open (a symlink, another owner, or group/world access); nothing in it is used", stagingDir)
	}

	raw, err := sys.ReadStaged(req.tokenFile, maxTokenBytes)
	if err != nil {
		return fmt.Errorf("read the staged enrollment token %s: %w", req.tokenFile, err)
	}
	tokenRemoved = true
	if rerr := sys.Remove(req.tokenFile); rerr != nil && !errors.Is(rerr, os.ErrNotExist) {
		return fmt.Errorf("remove the staged token %s: %w", req.tokenFile, rerr)
	}
	token := strings.TrimSpace(string(raw))
	zero(raw)
	if token == "" || strings.ContainsAny(token, " \t\r\n") {
		return errors.New("the staged enrollment token is empty or is not one line")
	}

	switch l.GOOS {
	case "linux":
		return linuxInstall(sys, l, req.enrollURL, token, exe)
	case "darwin":
		return darwinInstall(sys, l, req.enrollURL, token, exe)
	}
	return fmt.Errorf("there is no install mode for %q", l.GOOS)
}

// linuxInstall is agent/packaging/linux/install.sh from "state directory +
// token" to "service", step for step.
func linuxInstall(sys unixSystem, l UnixLayout, enrollURL, token, exe string) error {
	steps := []struct {
		what string
		do   func() error
	}{
		{"create " + l.DataDir, func() error { return sys.EnsureDir(l.DataDir, 0o700, true) }},
		{"write " + l.TokenPath, func() error { return sys.WriteExclusive(l.TokenPath, []byte(token), 0o600) }},
		{"create " + l.ConfDir, func() error { return sys.EnsureDir(l.ConfDir, 0o700, true) }},
		{"write " + l.EnvFile, func() error { return sys.WriteFile(l.EnvFile, linuxEnvFile(l, enrollURL), 0o600) }},
		{"create " + l.BinDir, func() error { return sys.EnsureDir(l.BinDir, 0o755, false) }},
		{"install " + l.Binary, func() error { return sys.CopyFile(exe, l.Binary, 0o755) }},
		{"create " + l.ServiceDir, func() error { return sys.EnsureDir(l.ServiceDir, 0o755, false) }},
		{"write " + l.ServiceFile, func() error { return sys.WriteFile(l.ServiceFile, []byte(linuxUnit), 0o644) }},
		{"systemctl daemon-reload", func() error { return sys.Run("systemctl", "daemon-reload") }},
		{"systemctl enable", func() error { return sys.Run("systemctl", "enable", l.Service+".service") }},
		{"systemctl restart", func() error { return sys.Run("systemctl", "restart", l.Service+".service") }},
	}
	for _, s := range steps {
		if err := s.do(); err != nil {
			return fmt.Errorf("%s: %w", s.what, err)
		}
	}
	return nil
}

// linuxEnvFile is what install.sh writes to /etc/its-agent/agent.env.
func linuxEnvFile(l UnixLayout, enrollURL string) []byte {
	return []byte("# Written by `its-agent install` (the SSH push), as agent/packaging/linux/install.sh\n" +
		"# writes it. Not a secret, and deliberately not where the enrollment token\n" +
		"# lives -- that is " + l.TokenPath + ".\n" +
		"ITS_AGENT_ENROLL_URL=" + enrollURL + "\n")
}

// darwinInstall is agent/packaging/darwin/install.sh, step for step. The token
// is always present here, so the job is always bootstrapped (install.sh skips
// that only for a Mac with neither a token nor an identity).
func darwinInstall(sys unixSystem, l UnixLayout, enrollURL, token, exe string) error {
	target := "system/" + l.Service
	steps := []struct {
		what string
		do   func() error
	}{
		{"create " + l.DataDir, func() error { return sys.EnsureDir(l.DataDir, 0o700, true) }},
		{"create " + l.LogDir, func() error { return sys.EnsureDir(l.LogDir, 0o750, true) }},
		{"create " + l.BinDir, func() error { return sys.EnsureDir(l.BinDir, 0o755, false) }},
		{"write " + l.TokenPath, func() error { return sys.WriteExclusive(l.TokenPath, []byte(token), 0o600) }},
		{"install " + l.Binary, func() error { return sys.CopyFile(exe, l.Binary, 0o755) }},
		{"create " + l.ServiceDir, func() error { return sys.EnsureDir(l.ServiceDir, 0o755, false) }},
		{"write " + l.ServiceFile, func() error { return sys.WriteFile(l.ServiceFile, darwinPlist(enrollURL), 0o644) }},
		// `|| true` in install.sh: nothing loaded yet is the normal case.
		{"launchctl bootout", func() error { _ = sys.Run("launchctl", "bootout", target); return nil }},
		// Enable BEFORE bootstrap: a label in launchd's disabled database
		// makes bootstrap fail (darwin/install.sh).
		{"launchctl enable", func() error { return sys.Run("launchctl", "enable", target) }},
		{"launchctl bootstrap", func() error { return sys.Run("launchctl", "bootstrap", "system", l.ServiceFile) }},
	}
	for _, s := range steps {
		if err := s.do(); err != nil {
			return fmt.Errorf("%s: %w", s.what, err)
		}
	}
	return nil
}

// darwinPlist is the shipped plist with ITS_AGENT_ENROLL_URL added to its
// EnvironmentVariables dict -- what darwin/install.sh's PlistBuddy `Add` does.
// enrollURL has passed layout.CheckEnrollOrigin, so it holds no character XML
// would read as markup.
func darwinPlist(enrollURL string) []byte {
	const anchor = "        <string>/var/lib/its-agent</string>\n"
	entry := "        <key>ITS_AGENT_ENROLL_URL</key>\n        <string>" + enrollURL + "</string>\n"
	return []byte(strings.Replace(darwinPlistTemplate, anchor, anchor+entry, 1))
}

// isStagedBinary reports whether exe IS the push's staged copy, comparing
// both sides with symlinks resolved (review Minor 5): os.Executable resolves
// them, so on a host where /opt is a symlink (`/opt -> var/opt`) the running
// staged binary reports /var/opt/toeshee/agent.pkg. A side that cannot be
// resolved is compared as given -- never guessed into a match.
func isStagedBinary(sys unixSystem, exe string) bool {
	if exe == layout.UnixStagedBinary {
		return true
	}
	resolvedExe, err1 := sys.EvalSymlinks(exe)
	resolvedStaged, err2 := sys.EvalSymlinks(layout.UnixStagedBinary)
	return err1 == nil && err2 == nil && resolvedExe == resolvedStaged
}

// parentDir is a slash path's directory ("/" for a top-level file).
func parentDir(path string) string {
	i := strings.LastIndexByte(path, '/')
	if i <= 0 {
		return "/"
	}
	return path[:i]
}

// zero overwrites a buffer that held the token.
func zero(b []byte) {
	for i := range b {
		b[i] = 0
	}
}

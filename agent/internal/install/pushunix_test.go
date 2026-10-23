package install

import (
	"bytes"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/install/layout"
)

// The Linux and macOS install mode the SSH push runs on the target
// (`its-agent install --token-file ... --enroll-url ...`), exercised as a
// plan against a fake system: every file operation and every systemctl /
// launchctl call is recorded, so the ORDER and the MODES are asserted, not
// inferred. The real system (push_unix.go) is a thin layer over os and
// os/exec, tested on its own for the two operations with a security property
// (push_unix_test.go).

const testOrigin = "https://itms.example.com"

type fakeUnix struct {
	euid   int
	files  map[string][]byte // what exists, by path
	dirs   map[string]stagingDirInfo
	links  map[string]string // path -> what filepath.EvalSymlinks returns
	ops    []string
	runErr map[string]error // "systemctl restart its-agent.service" -> err
}

func newFakeUnix() *fakeUnix {
	return &fakeUnix{
		files: map[string][]byte{
			layout.UnixStagedToken:  []byte("PUSH-TOKEN\n"),
			layout.UnixStagedBinary: []byte("the agent binary"),
		},
		dirs: map[string]stagingDirInfo{
			layout.UnixPushStagingDir: {IsDir: true, UID: 0, Mode: 0o700},
		},
	}
}

func (f *fakeUnix) EvalSymlinks(path string) (string, error) {
	if real, ok := f.links[path]; ok {
		return real, nil
	}
	return path, nil
}

func (f *fakeUnix) LstatDir(path string) (stagingDirInfo, error) {
	f.ops = append(f.ops, "lstat "+path)
	info, ok := f.dirs[path]
	if !ok {
		return stagingDirInfo{}, os.ErrNotExist
	}
	return info, nil
}

func (f *fakeUnix) Geteuid() int { return f.euid }

func (f *fakeUnix) ReadStaged(path string, limit int64) ([]byte, error) {
	f.ops = append(f.ops, "read "+path)
	b, ok := f.files[path]
	if !ok {
		return nil, os.ErrNotExist
	}
	if int64(len(b)) > limit {
		return nil, errors.New("too large")
	}
	return append([]byte(nil), b...), nil
}

func (f *fakeUnix) EnsureDir(path string, mode os.FileMode, rootOwned bool) error {
	f.ops = append(f.ops, fmt.Sprintf("dir %s %#o root=%t", path, mode, rootOwned))
	return nil
}

func (f *fakeUnix) WriteFile(path string, data []byte, mode os.FileMode) error {
	f.ops = append(f.ops, fmt.Sprintf("write %s %#o", path, mode))
	f.files[path] = append([]byte(nil), data...)
	return nil
}

func (f *fakeUnix) WriteExclusive(path string, data []byte, mode os.FileMode) error {
	f.ops = append(f.ops, fmt.Sprintf("excl %s %#o", path, mode))
	f.files[path] = append([]byte(nil), data...)
	return nil
}

func (f *fakeUnix) CopyFile(src, dst string, mode os.FileMode) error {
	f.ops = append(f.ops, fmt.Sprintf("copy %s %s %#o", src, dst, mode))
	b, ok := f.files[src]
	if !ok {
		return os.ErrNotExist
	}
	f.files[dst] = append([]byte(nil), b...)
	return nil
}

func (f *fakeUnix) Remove(path string) error {
	f.ops = append(f.ops, "remove "+path)
	delete(f.files, path)
	return nil
}

func (f *fakeUnix) Run(name string, args ...string) error {
	line := strings.Join(append([]string{name}, args...), " ")
	f.ops = append(f.ops, "run "+line)
	return f.runErr[line]
}

func pushArgs() []string {
	return []string{layout.FlagTokenFile, layout.UnixStagedToken, layout.FlagEnrollURL, testOrigin}
}

func mustLayout(t *testing.T, goos string) UnixLayout {
	t.Helper()
	l, err := UnixLayoutFor(goos)
	if err != nil {
		t.Fatal(err)
	}
	return l
}

// --------------------------------------------------------------------------- //
// The layouts are the shipped install.sh's, not a second one
// --------------------------------------------------------------------------- //

func TestTheLinuxLayoutIsInstallShs(t *testing.T) {
	want := UnixLayout{
		GOOS:        "linux",
		BinDir:      "/usr/local/bin",
		Binary:      "/usr/local/bin/its-agent",
		DataDir:     "/var/lib/its-agent",
		TokenPath:   "/var/lib/its-agent/enroll-token",
		ConfDir:     "/etc/its-agent",
		EnvFile:     "/etc/its-agent/agent.env",
		ServiceDir:  "/etc/systemd/system",
		ServiceFile: "/etc/systemd/system/its-agent.service",
		Service:     "its-agent",
	}
	if got := mustLayout(t, "linux"); !reflect.DeepEqual(got, want) {
		t.Fatalf("linux layout =\n%+v\nwant\n%+v", got, want)
	}
	script := readPackaging(t, "linux/install.sh")
	for _, line := range []string{
		`BINDIR="/usr/local/bin"`, `UNITDIR="/etc/systemd/system"`, `CONFDIR="/etc/its-agent"`,
		`STATEDIR="/var/lib/its-agent"`, `SERVICE="its-agent"`, `envfile="${DESTDIR}${CONFDIR}/agent.env"`,
		`tokenfile="${DESTDIR}${STATEDIR}/enroll-token"`, `ITS_AGENT_ENROLL_URL=${URL}`,
	} {
		if !strings.Contains(script, line) {
			t.Errorf("linux/install.sh no longer says %s; the install mode copies it", line)
		}
	}
}

func TestTheDarwinLayoutIsInstallShs(t *testing.T) {
	want := UnixLayout{
		GOOS:        "darwin",
		BinDir:      "/usr/local/libexec",
		Binary:      "/usr/local/libexec/its-agent",
		DataDir:     "/var/lib/its-agent",
		TokenPath:   "/var/lib/its-agent/enroll-token",
		LogDir:      "/var/log/its-agent",
		ServiceDir:  "/Library/LaunchDaemons",
		ServiceFile: "/Library/LaunchDaemons/com.toeshee.itsagent.plist",
		Service:     "com.toeshee.itsagent",
	}
	if got := mustLayout(t, "darwin"); !reflect.DeepEqual(got, want) {
		t.Fatalf("darwin layout =\n%+v\nwant\n%+v", got, want)
	}
	script := readPackaging(t, "darwin/install.sh")
	for _, line := range []string{
		`BINDIR="/usr/local/libexec"`, `PLISTDIR="/Library/LaunchDaemons"`, `STATEDIR="/var/lib/its-agent"`,
		`LOGDIR="/var/log/its-agent"`, `LABEL="com.toeshee.itsagent"`,
		`Add :EnvironmentVariables:ITS_AGENT_ENROLL_URL string ${URL}`,
	} {
		if !strings.Contains(script, line) {
			t.Errorf("darwin/install.sh no longer says %s; the install mode copies it", line)
		}
	}
}

func TestOtherSystemsHaveNoInstallMode(t *testing.T) {
	for _, goos := range []string{"windows", "freebsd", ""} {
		if _, err := UnixLayoutFor(goos); err == nil {
			t.Errorf("UnixLayoutFor(%q) returned a layout", goos)
		}
	}
}

// The unit and the plist are the SHIPPED files, byte for byte: the install
// mode carries copies because the service must not link `embed` for the
// installer's sake (imports_test.go), and this test is what keeps them equal.
func TestTheServiceDefinitionsAreTheShippedFiles(t *testing.T) {
	if got, want := linuxUnit, readPackaging(t, "linux/its-agent.service"); got != want {
		t.Fatalf("linuxUnit differs from agent/packaging/linux/its-agent.service (%d vs %d bytes); "+
			"copy the file into pushunix.go", len(got), len(want))
	}
	if got, want := darwinPlistTemplate, readPackaging(t, "darwin/com.toeshee.itsagent.plist"); got != want {
		t.Fatalf("darwinPlistTemplate differs from agent/packaging/darwin/com.toeshee.itsagent.plist (%d vs %d bytes)",
			len(got), len(want))
	}
}

func readPackaging(t *testing.T, rel string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "..", "packaging", filepath.FromSlash(rel)))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// --------------------------------------------------------------------------- //
// Linux: install.sh's steps, in install.sh's order
// --------------------------------------------------------------------------- //

func TestLinuxPushInstallFollowsInstallSh(t *testing.T) {
	f := newFakeUnix()
	if err := pushInstall(f, mustLayout(t, "linux"), pushArgs(), layout.UnixStagedBinary); err != nil {
		t.Fatal(err)
	}
	want := []string{
		// Review I3: the staging directory is checked before anything in it
		// is read.
		"lstat " + layout.UnixPushStagingDir,
		"read " + layout.UnixStagedToken,
		// The staged token is removed the moment it has been read.
		"remove " + layout.UnixStagedToken,
		"dir /var/lib/its-agent 0700 root=true",
		"excl /var/lib/its-agent/enroll-token 0600",
		"dir /etc/its-agent 0700 root=true",
		"write /etc/its-agent/agent.env 0600",
		"dir /usr/local/bin 0755 root=false",
		"copy " + layout.UnixStagedBinary + " /usr/local/bin/its-agent 0755",
		"dir /etc/systemd/system 0755 root=false",
		"write /etc/systemd/system/its-agent.service 0644",
		"run systemctl daemon-reload",
		"run systemctl enable its-agent.service",
		"run systemctl restart its-agent.service",
		"remove " + layout.UnixStagedBinary,
	}
	if !reflect.DeepEqual(f.ops, want) {
		t.Fatalf("ops =\n%s\nwant\n%s", strings.Join(f.ops, "\n"), strings.Join(want, "\n"))
	}
	if got := string(f.files["/var/lib/its-agent/enroll-token"]); got != "PUSH-TOKEN" {
		t.Fatalf("token file = %q", got)
	}
	env := string(f.files["/etc/its-agent/agent.env"])
	if !strings.Contains(env, "\nITS_AGENT_ENROLL_URL="+testOrigin+"\n") {
		t.Fatalf("agent.env = %q", env)
	}
	if strings.Contains(env, "PUSH-TOKEN") {
		t.Fatal("the token is in agent.env")
	}
	if got := string(f.files["/etc/systemd/system/its-agent.service"]); got != linuxUnit {
		t.Fatal("the unit written is not the shipped unit")
	}
	for _, staged := range []string{layout.UnixStagedToken, layout.UnixStagedBinary} {
		if _, ok := f.files[staged]; ok {
			t.Errorf("%s was left behind", staged)
		}
	}
}

// --------------------------------------------------------------------------- //
// macOS: darwin/install.sh's steps, in its order
// --------------------------------------------------------------------------- //

func TestDarwinPushInstallFollowsInstallSh(t *testing.T) {
	f := newFakeUnix()
	if err := pushInstall(f, mustLayout(t, "darwin"), pushArgs(), layout.UnixStagedBinary); err != nil {
		t.Fatal(err)
	}
	plist := "/Library/LaunchDaemons/com.toeshee.itsagent.plist"
	want := []string{
		// Review I3: the staging directory is checked before anything in it
		// is read.
		"lstat " + layout.UnixPushStagingDir,
		"read " + layout.UnixStagedToken,
		"remove " + layout.UnixStagedToken,
		"dir /var/lib/its-agent 0700 root=true",
		"dir /var/log/its-agent 0750 root=true",
		"dir /usr/local/libexec 0755 root=false",
		"excl /var/lib/its-agent/enroll-token 0600",
		"copy " + layout.UnixStagedBinary + " /usr/local/libexec/its-agent 0755",
		"dir /Library/LaunchDaemons 0755 root=false",
		"write " + plist + " 0644",
		"run launchctl bootout system/com.toeshee.itsagent",
		// Enable BEFORE bootstrap (darwin/install.sh says why).
		"run launchctl enable system/com.toeshee.itsagent",
		"run launchctl bootstrap system " + plist,
		"remove " + layout.UnixStagedBinary,
	}
	if !reflect.DeepEqual(f.ops, want) {
		t.Fatalf("ops =\n%s\nwant\n%s", strings.Join(f.ops, "\n"), strings.Join(want, "\n"))
	}
	body := f.files[plist]
	env := plistEnvironment(t, body)
	if env["ITS_AGENT_ENROLL_URL"] != testOrigin || env["ITS_AGENT_DATA_DIR"] != "/var/lib/its-agent" {
		t.Fatalf("plist EnvironmentVariables = %v", env)
	}
	if bytes.Contains(body, []byte("PUSH-TOKEN")) {
		t.Fatal("the token is in the plist")
	}
}

func TestADarwinBootoutFailureIsNotFatal(t *testing.T) {
	// install.sh: `launchctl bootout ... || true` -- nothing loaded is normal.
	f := newFakeUnix()
	f.runErr = map[string]error{"launchctl bootout system/com.toeshee.itsagent": errors.New("no such service")}
	if err := pushInstall(f, mustLayout(t, "darwin"), pushArgs(), layout.UnixStagedBinary); err != nil {
		t.Fatal(err)
	}
}

// plistEnvironment decodes the EnvironmentVariables dict of a launchd plist.
func plistEnvironment(t *testing.T, body []byte) map[string]string {
	t.Helper()
	dec := xml.NewDecoder(bytes.NewReader(body))
	dec.Strict = true
	dec.Entity = xml.HTMLEntity
	env := map[string]string{}
	var path []string
	var key, text string
	inEnv := false
	for {
		tok, err := dec.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("the plist is not well-formed XML: %v", err)
		}
		switch v := tok.(type) {
		case xml.StartElement:
			path = append(path, v.Name.Local)
			text = ""
		case xml.CharData:
			text += string(v)
		case xml.EndElement:
			path = path[:len(path)-1]
			switch {
			case v.Name.Local == "key" && len(path) == 2 && text == "EnvironmentVariables":
				inEnv = true
			case v.Name.Local == "key" && inEnv && len(path) == 3:
				key = text
			case v.Name.Local == "string" && inEnv && len(path) == 3:
				env[key] = text
			case v.Name.Local == "dict" && inEnv && len(path) == 2:
				inEnv = false
			}
		}
	}
	return env
}

// --------------------------------------------------------------------------- //
// Refusals: nothing is installed
// --------------------------------------------------------------------------- //

func TestPushInstallRefusesBeforeTouchingTheSystem(t *testing.T) {
	cases := map[string][]string{
		"the token as an argument": {"--token", "PUSH-TOKEN", layout.FlagEnrollURL, testOrigin},
		"--token=":                 {"--token=PUSH-TOKEN", layout.FlagEnrollURL, testOrigin},
		"no token file":            {layout.FlagEnrollURL, testOrigin},
		"no enroll URL":            {layout.FlagTokenFile, layout.UnixStagedToken},
		"an http enroll URL":       {layout.FlagTokenFile, layout.UnixStagedToken, layout.FlagEnrollURL, "http://itms.example.com"},
		"a path in the enroll URL": {layout.FlagTokenFile, layout.UnixStagedToken, layout.FlagEnrollURL, testOrigin + "/enroll/v1"},
		"an unknown flag":          append(pushArgs(), "--force"),
		"a stray argument":         append(pushArgs(), "extra"),
		"a repeated flag":          append(pushArgs(), layout.FlagEnrollURL, testOrigin),
		"the token file is the final token path": {
			layout.FlagTokenFile, "/var/lib/its-agent/enroll-token", layout.FlagEnrollURL, testOrigin,
		},
	}
	for name, args := range cases {
		for _, goos := range []string{"linux", "darwin"} {
			f := newFakeUnix()
			err := pushInstall(f, mustLayout(t, goos), args, layout.UnixStagedBinary)
			if err == nil {
				t.Errorf("%s/%s: accepted", goos, name)
			} else if strings.Contains(err.Error(), "PUSH-TOKEN") {
				t.Errorf("%s/%s: the error echoes the token: %v", goos, name, err)
			}
			if len(f.ops) != 0 {
				t.Errorf("%s/%s: touched the system before refusing: %v", goos, name, f.ops)
			}
		}
	}
}

func TestPushInstallRefusesANonRootCallerButStillRemovesTheStagedToken(t *testing.T) {
	f := newFakeUnix()
	f.euid = 1000
	err := pushInstall(f, mustLayout(t, "linux"), pushArgs(), layout.UnixStagedBinary)
	if err == nil || !strings.Contains(err.Error(), "root") {
		t.Fatalf("err = %v", err)
	}
	want := []string{"remove " + layout.UnixStagedToken, "remove " + layout.UnixStagedBinary}
	// The root check comes first: an unprivileged caller learns nothing about
	// the staging directory, and the staged files are still removed.
	if !reflect.DeepEqual(f.ops, want) {
		t.Fatalf("ops = %v, want only the staged files removed", f.ops)
	}
}

func TestPushInstallRefusesAMissingOrEmptyToken(t *testing.T) {
	for name, content := range map[string][]byte{"missing": nil, "empty": []byte(" \n"), "two lines": []byte("a\nb")} {
		f := newFakeUnix()
		if content == nil {
			delete(f.files, layout.UnixStagedToken)
		} else {
			f.files[layout.UnixStagedToken] = content
		}
		err := pushInstall(f, mustLayout(t, "linux"), pushArgs(), layout.UnixStagedBinary)
		if err == nil {
			t.Errorf("%s token: accepted", name)
		}
		for _, op := range f.ops {
			if strings.HasPrefix(op, "excl ") || strings.HasPrefix(op, "copy ") || strings.HasPrefix(op, "run ") {
				t.Errorf("%s token: %s ran", name, op)
			}
		}
		if _, ok := f.files[layout.UnixStagedBinary]; ok {
			t.Errorf("%s token: the staged binary was left behind", name)
		}
	}
}

func TestAFailedServiceStartIsReportedAndTheStagedFilesAreStillRemoved(t *testing.T) {
	f := newFakeUnix()
	f.runErr = map[string]error{"systemctl restart its-agent.service": errors.New("exit status 1")}
	err := pushInstall(f, mustLayout(t, "linux"), pushArgs(), layout.UnixStagedBinary)
	if err == nil || !strings.Contains(err.Error(), "restart") {
		t.Fatalf("err = %v", err)
	}
	for _, staged := range []string{layout.UnixStagedToken, layout.UnixStagedBinary} {
		if _, ok := f.files[staged]; ok {
			t.Errorf("%s was left behind after a failure", staged)
		}
	}
}

func TestOnlyAStagedBinaryIsDeleted(t *testing.T) {
	// Run as the INSTALLED binary (or anything outside the push staging
	// directory), the install mode must never delete the file it runs from.
	for _, exe := range []string{"/usr/local/bin/its-agent", "/home/op/its-agent"} {
		f := newFakeUnix()
		f.files[exe] = []byte("the agent binary")
		if err := pushInstall(f, mustLayout(t, "linux"), pushArgs(), exe); err != nil {
			t.Fatal(err)
		}
		for _, op := range f.ops {
			if op == "remove "+exe {
				t.Errorf("deleted %s", exe)
			}
		}
	}
}

func TestTheTokenNeverReachesAnArgumentOrALog(t *testing.T) {
	f := newFakeUnix()
	if err := pushInstall(f, mustLayout(t, "linux"), pushArgs(), layout.UnixStagedBinary); err != nil {
		t.Fatal(err)
	}
	for _, op := range f.ops {
		if strings.Contains(op, "PUSH-TOKEN") {
			t.Fatalf("the token appears in an operation: %q", op)
		}
	}
}

// REVIEW I3. The staging directory the token and binary were placed in must be
// a real directory (not a symlink), owned by root, with no group or world
// access -- checked before anything in it is read. A refusal touches nothing
// in it, not even to clean up: a directory someone else controls is not one
// to delete through.
func TestPushInstallRefusesAStagingDirectorySomeoneElseCouldWrite(t *testing.T) {
	for name, info := range map[string]stagingDirInfo{
		"a symlink":       {IsSymlink: true, UID: 0, Mode: 0o700},
		"not a directory": {IsDir: false, UID: 0, Mode: 0o700},
		"owned by a user": {IsDir: true, UID: 1000, Mode: 0o700},
		"group-readable":  {IsDir: true, UID: 0, Mode: 0o750},
		"world-writable":  {IsDir: true, UID: 0, Mode: 0o777},
		"sticky and 1777": {IsDir: true, UID: 0, Mode: 0o1777},
	} {
		f := newFakeUnix()
		f.dirs[layout.UnixPushStagingDir] = info
		err := pushInstall(f, mustLayout(t, "linux"), pushArgs(), layout.UnixStagedBinary)
		if err == nil || !strings.Contains(err.Error(), layout.UnixPushStagingDir) {
			t.Errorf("%s: err = %v", name, err)
		}
		if want := []string{"lstat " + layout.UnixPushStagingDir}; !reflect.DeepEqual(f.ops, want) {
			t.Errorf("%s: ops = %v, want only the check", name, f.ops)
		}
	}
	f := newFakeUnix()
	delete(f.dirs, layout.UnixPushStagingDir)
	if err := pushInstall(f, mustLayout(t, "linux"), pushArgs(), layout.UnixStagedBinary); err == nil {
		t.Error("a missing staging directory was accepted")
	}
}

// REVIEW Minor 5. os.Executable resolves symlinks, so on a host where /opt is
// a symlink (`/opt -> var/opt`) the running staged binary reports
// /var/opt/toeshee/agent.pkg. Both sides are resolved before comparing, or
// the staged copy would be left behind.
func TestTheStagedBinaryIsDeletedThroughASymlinkedOpt(t *testing.T) {
	f := newFakeUnix()
	real := "/var/opt/toeshee/agent.pkg"
	f.links = map[string]string{layout.UnixStagedBinary: real}
	f.files[real] = []byte("the agent binary")
	if err := pushInstall(f, mustLayout(t, "linux"), pushArgs(), real); err != nil {
		t.Fatal(err)
	}
	if _, ok := f.files[real]; ok {
		t.Fatalf("the staged binary %s was left behind; ops = %v", real, f.ops)
	}
}

func TestAFileThatMerelyResolvesElsewhereIsNotDeleted(t *testing.T) {
	// The installed binary is a symlink to somewhere else entirely: still not
	// the staged copy, still never deleted.
	f := newFakeUnix()
	exe := "/usr/local/bin/its-agent"
	f.links = map[string]string{exe: "/usr/local/lib/its-agent-1.6.0"}
	f.files[exe] = []byte("the agent binary")
	if err := pushInstall(f, mustLayout(t, "linux"), pushArgs(), exe); err != nil {
		t.Fatal(err)
	}
	for _, op := range f.ops {
		if op == "remove "+exe || op == "remove /usr/local/lib/its-agent-1.6.0" {
			t.Fatalf("deleted the running binary: %s", op)
		}
	}
}

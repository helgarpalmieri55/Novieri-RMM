//go:build e2e && windows

package e2e

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/hex"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"
	"time"
)

// TestInstallLinkWindows is spec §5 Part 1's Windows row: the self-installer
// installs the service, the service enrolls through the link code, and
// `agent.uninstall` removes it cleanly -- on a real Windows kernel and a real
// Service Control Manager.
//
// The console comes from the FILE NAME (the operator's decision of 2026-09-28,
// replacing plan D6's Mark of the Web): `ITMS-Agent-Setup_<host>@<port>_<code>`.
// The file here carries NO Zone.Identifier stream, which is the "copied from a
// share or a USB stick" case that decision exists for.
func TestInstallLinkWindows(t *testing.T) {
	binDir := os.Getenv("E2E_BIN_DIR")
	if binDir == "" {
		t.Fatal("E2E_BIN_DIR is unset; run internal/update/e2e/build.sh first")
	}
	repoRoot, err := filepath.Abs(filepath.Join("..", "..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	if v := os.Getenv("E2E_REPO_ROOT"); v != "" {
		repoRoot = v
	}
	const code = "e2eLINKcode_0123456789-ABCDEFGHI"

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

	// The link offers release A (review I1): its digest, signed with
	// build.sh's throwaway release key, which every e2e binary pins.
	binA, err := os.ReadFile(filepath.Join(binDir, "A"+exe()))
	if err != nil {
		t.Fatal(err)
	}
	enroll.offer(t, code, versionA, binA, loadReleaseKey(t, filepath.Join(binDir, "release-key.pem")))

	// The download, as a browser leaves it: the published binary under the
	// link's name -- the console's host and port, then the code -- with
	// Chrome's " (1)". `https://localhost:<port>` becomes `localhost@<port>`;
	// the enrollment server's certificate names localhost and chains to the
	// CA trusted above, so TLS is verified exactly as in the field.
	authority := strings.Replace(strings.TrimPrefix(enroll.url, "https://"), ":", "@", 1)
	downloads := t.TempDir()

	// NEGATIVE 1: a name whose host is not a DNS name is not an installer. The
	// binary starts as a plain agent with nothing to enroll with, exits
	// non-zero, and installs nothing.
	bogus := filepath.Join(downloads, "ITMS-Agent-Setup_10.0.0.1_"+code+".exe")
	copyFileT(t, filepath.Join(binDir, "A"+exe()), bogus)
	out, exitErr := runInstaller(t, bogus, "ITS_AGENT_DATA_DIR="+t.TempDir(), "ITS_AGENT_ENROLL_URL=", "ITS_AGENT_ENROLL_TOKEN=")
	if exitErr == nil || strings.Contains(out, "Installed") {
		t.Fatalf("an invalid-host name: err=%v\n%s", exitErr, out)
	}
	assertNoService(t)

	// NEGATIVE 2 (review I1): the right name on the wrong bytes -- binary B,
	// validly signed, but not the release the link offers. The installer
	// refuses before the token is written or the service touched.
	wrong := filepath.Join(downloads, "ITMS-Agent-Setup_"+authority+"_"+code+".exe")
	copyFileT(t, filepath.Join(binDir, "B"+exe()), wrong)
	out, exitErr = runInstaller(t, wrong)
	if exitErr == nil || !strings.Contains(out, "could not be confirmed") {
		t.Fatalf("a file that is not the offered release: err=%v\n%s", exitErr, out)
	}
	assertNoService(t)
	if _, err := os.Stat(filepath.Join(plat.dataDir(), "enroll-token")); !os.IsNotExist(err) {
		t.Fatalf("a refused install left a token file: %v", err)
	}

	setup := filepath.Join(downloads, "ITMS-Agent-Setup_"+authority+"_"+code+" (1).exe")
	copyFileT(t, filepath.Join(binDir, "A"+exe()), setup)

	// NEGATIVE 3 (2026-09-29): %ProgramData%\ITSuite, the data directory's
	// PARENT, pre-created by a standard user. Owning it is FILE_DELETE_CHILD:
	// after the install they could rename the protected `agent` directory
	// away and put their own identity in its place. The right file, the right
	// release -- and a fresh install refuses, leaving the directory as it
	// found it for an administrator to look at.
	refuseForeignOwnedParent(t, setup, plat)

	// Double-clicked, in effect: no arguments. The runner is already elevated,
	// so there is no UAC prompt, and EnvNoUI replaces the window (and the host
	// question) with stderr.
	out, exitErr = runInstaller(t, setup)
	if exitErr != nil || !strings.Contains(out, "Installed") {
		t.Fatalf("the self-installer failed: %v\n%s", exitErr, out)
	}

	// Installed where install.ps1 installs, as a service with NO arguments.
	if got := plat.configuredBinary(t); !strings.EqualFold(got, plat.target()) {
		t.Fatalf("service binary = %q, want %q", got, plat.target())
	}
	pathName := ps(t, "(Get-CimInstance Win32_Service -Filter \"Name='its-agent'\").PathName")
	if strings.TrimSpace(pathName) != `"`+plat.target()+`"` {
		t.Fatalf("service command line = %q; it must be the quoted path and nothing else", pathName)
	}
	env := ps(t, "(Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\its-agent').Environment -join '|'")
	if !strings.Contains(env, "ITS_AGENT_ENROLL_URL="+enroll.url) || strings.Contains(env, code) {
		t.Fatalf("service environment = %q", env)
	}

	// Enrolled through the link, and connected.
	c := gw.waitVersion(versionA, 3*time.Minute)
	bodies := enroll.requests()
	if len(bodies) != 1 || bodies[0]["link_code"] != code {
		t.Fatalf("enrollment requests = %v; want one carrying link_code", bodies)
	}
	if _, hasToken := bodies[0]["token"]; hasToken {
		t.Fatal("the enrollment carried a token member as well as the link code")
	}
	if !strings.Contains(c.caps, "agent.uninstall") {
		t.Fatalf("advertised capabilities %q lack agent.uninstall", c.caps)
	}
	if _, err := os.Stat(filepath.Join(plat.dataDir(), "enroll-token")); !os.IsNotExist(err) {
		t.Fatalf("the token file survived enrollment: %v", err)
	}
	// Review I7: what the installer set, read back from the machine.
	// The data directory: owned by Administrators (review I4), and exactly
	// install.ps1's protected DACL -- nobody else, nothing inherited.
	// "D:P" when the installer created it with its descriptor
	// (secdir.MkdirOwned), "D:PAI" when it applied one to a directory that
	// already existed; the same protected pair either way.
	assertOwnedSDDL(t, "data directory", plat.dataDir())
	// Its PARENT, %ProgramData%\ITSuite, identically: owned by Administrators,
	// SYSTEM and Administrators only, nothing inherited from ProgramData
	// (whose BUILTIN\Users may create folders).
	assertOwnedSDDL(t, "data directory's parent", filepath.Dir(plat.dataDir()))
	// The private key the service wrote inherits exactly those two.
	who := strings.Fields(strings.ReplaceAll(ps(t, "(Get-Acl -LiteralPath '"+
		filepath.Join(plat.dataDir(), "agent.key")+"').Access | ForEach-Object { $_.IdentityReference.Value.Replace(' ', '_') }"), "\r", ""))
	sort.Strings(who)
	if !reflect.DeepEqual(who, []string{"BUILTIN\\Administrators", "NT_AUTHORITY\\SYSTEM"}) {
		t.Fatalf("agent.key is accessible to %v; want SYSTEM and Administrators only", who)
	}
	// Recovery: install.ps1's `reset= 300 actions= restart/5000/restart/15000/restart/60000`.
	qf := run(t, "sc.exe", "qfailure", "its-agent")
	if !regexp.MustCompile(`RESET_PERIOD[^:]*:\s*300\b`).MatchString(qf) {
		t.Fatalf("sc.exe qfailure: no RESET_PERIOD 300\n%s", qf)
	}
	var delays []string
	for _, m := range regexp.MustCompile(`Delay = (\d+) milliseconds`).FindAllStringSubmatch(qf, -1) {
		delays = append(delays, m[1])
	}
	if !reflect.DeepEqual(delays, []string{"5000", "15000", "60000"}) {
		t.Fatalf("sc.exe qfailure delays = %v, want [5000 15000 60000]\n%s", delays, qf)
	}

	// No running its-agent process carries the code on its command line.
	lines := ps(t, "Get-CimInstance Win32_Process -Filter \"Name='its-agent.exe'\" | ForEach-Object { $_.CommandLine }")
	if strings.Contains(lines, code) {
		t.Fatalf("a running agent carries the code in its command line: %s", lines)
	}

	// Reject: the signed uninstall.
	id := gw.send("agent.uninstall", map[string]any{})
	res := gw.waitResult(id, 2*time.Minute)
	if res.State != "succeeded" || res.Result["scheduled"] != true {
		t.Fatalf("agent.uninstall result = %+v, want succeeded/scheduled", res)
	}
	deadline := time.Now().Add(3 * time.Minute)
	for {
		svc := strings.TrimSpace(ps(t, "(Get-Service -Name its-agent -ErrorAction SilentlyContinue).Name"))
		_, binErr := os.Stat(plat.target())
		_, dataErr := os.Stat(plat.dataDir())
		if svc == "" && os.IsNotExist(binErr) && os.IsNotExist(dataErr) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("not removed within 3 minutes: service=%q binary=%v data=%v", svc, binErr, dataErr)
		}
		time.Sleep(2 * time.Second)
	}
}

// assertOwnedSDDL reads dir's descriptor back: owner BA and exactly the
// protected SYSTEM+Administrators DACL, as D:P or D:PAI (secdir's own test
// accepts both, for the reason given there).
func assertOwnedSDDL(t *testing.T, what, dir string) {
	t.Helper()
	sddl := strings.TrimSpace(ps(t, "(Get-Acl -LiteralPath '"+dir+"').Sddl"))
	owner, dacl := sddlParts(sddl)
	if owner != "BA" || (dacl != "P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)" && dacl != "PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)") {
		t.Fatalf("%s %s SDDL = %q; want owner BA and DACL D:P or D:PAI (A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", what, dir, sddl)
	}
}

// refuseForeignOwnedParent is NEGATIVE 3. The runner is elevated and cannot
// cheaply run the mkdir as a standard user, so the standard user's ownership
// is given directly: a local non-administrator account is created for the
// test and made ITSuite's owner with `icacls /setowner` (which uses the
// SeRestorePrivilege an elevated token holds). What the installer sees is
// what `mkdir C:\ProgramData\ITSuite` by that user leaves behind: a directory
// the user owns.
func refuseForeignOwnedParent(t *testing.T, setup string, plat platform) {
	t.Helper()
	parent := filepath.Dir(plat.dataDir())
	const user = "itms-e2e-std"
	// net user asks for confirmation above 14 characters; this is 14, and
	// meets the default complexity rules.
	suffix := make([]byte, 5)
	if _, err := rand.Read(suffix); err != nil {
		t.Fatal(err)
	}
	password := "Aa1!" + hex.EncodeToString(suffix)
	_, _ = runQuiet("net", "user", user, "/delete")
	run(t, "net", "user", user, password, "/add")
	t.Cleanup(func() { _, _ = runQuiet("net", "user", user, "/delete") })

	_, statErr := os.Stat(parent)
	created := os.IsNotExist(statErr)
	if created {
		if err := os.MkdirAll(parent, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	restored := false
	restore := func() {
		if restored {
			return
		}
		restored = true
		if created {
			_ = os.RemoveAll(parent)
		} else {
			_, _ = runQuiet("icacls", parent, "/setowner", "*S-1-5-32-544")
		}
	}
	t.Cleanup(restore)
	run(t, "icacls", parent, "/setowner", user)
	ownerOf := func() string {
		return strings.TrimSpace(ps(t, "(Get-Acl -LiteralPath '"+parent+"').Owner"))
	}
	if o := ownerOf(); !strings.HasSuffix(strings.ToLower(o), `\`+user) {
		t.Fatalf("could not make %s the owner of %s (owner is %q)", user, parent, o)
	}

	out, exitErr := runInstaller(t, setup)
	if exitErr == nil || !strings.Contains(out, "not to an administrator") || strings.Contains(out, "Installed") {
		t.Fatalf("a fresh install under an ITSuite owned by %s: err=%v\n%s", user, exitErr, out)
	}
	assertNoService(t)
	if o := ownerOf(); !strings.HasSuffix(strings.ToLower(o), `\`+user) {
		t.Fatalf("the refused ITSuite was taken over (owner now %q); it must be left for an administrator", o)
	}
	if created {
		if _, err := os.Stat(plat.dataDir()); !os.IsNotExist(err) {
			t.Fatalf("a data directory was created under the refused parent: %v", err)
		}
	}
	if _, err := os.Stat(filepath.Join(plat.dataDir(), "enroll-token")); !os.IsNotExist(err) {
		t.Fatalf("a refused install left a token file: %v", err)
	}
	restore()
}

// runInstaller starts path with no arguments, EnvNoUI and any extra
// environment, bounded so a binary that did NOT take installer mode and sat
// waiting cannot hang the job.
func runInstaller(t *testing.T, path string, env ...string) (string, error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, path)
	cmd.Env = append(append(os.Environ(), "ITS_AGENT_INSTALLER_NO_UI=1"), env...)
	out, err := cmd.CombinedOutput()
	if ctx.Err() != nil {
		t.Fatalf("%s did not finish within 3 minutes\n%s", filepath.Base(path), out)
	}
	return string(out), err
}

func assertNoService(t *testing.T) {
	t.Helper()
	if svc := strings.TrimSpace(ps(t, "(Get-Service -Name its-agent -ErrorAction SilentlyContinue).Name")); svc != "" {
		t.Fatalf("an its-agent service exists: %q", svc)
	}
}

// sddlParts returns an SDDL string's owner and DACL (without "O:"/"D:").
func sddlParts(sddl string) (owner, dacl string) {
	if i := strings.Index(sddl, "O:"); i >= 0 {
		owner = sddl[i+2:]
		for j := 0; j+1 < len(owner); j++ {
			if strings.IndexByte("GDS", owner[j]) >= 0 && owner[j+1] == ':' {
				owner = owner[:j]
				break
			}
		}
	}
	if i := strings.Index(sddl, "D:"); i >= 0 {
		dacl = sddl[i+2:]
		if j := strings.Index(dacl, "S:"); j >= 0 {
			dacl = dacl[:j]
		}
	}
	return owner, dacl
}

func copyFileT(t *testing.T, src, dst string) {
	t.Helper()
	in, err := os.Open(src)
	if err != nil {
		t.Fatal(err)
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := io.Copy(out, in); err != nil {
		t.Fatal(err)
	}
	if err := out.Close(); err != nil {
		t.Fatal(err)
	}
}

//go:build linux || darwin

package install

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The real system's operations that carry a security property, on a temporary
// directory. chown is off so the tests run unprivileged; RunPush turns it on.

func TestReadStagedRefusesASymlinkADirectoryAndAnOversizedFile(t *testing.T) {
	dir := t.TempDir()
	sys := realUnix{}
	secret := filepath.Join(dir, "secret")
	if err := os.WriteFile(secret, []byte("not the token"), 0o600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(secret, link); err != nil {
		t.Fatal(err)
	}
	if _, err := sys.ReadStaged(link, 4096); err == nil {
		t.Error("read the token through a symlink")
	}
	if _, err := sys.ReadStaged(dir, 4096); err == nil {
		t.Error("read a directory as the token")
	}
	big := filepath.Join(dir, "big")
	if err := os.WriteFile(big, []byte(strings.Repeat("x", 5000)), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := sys.ReadStaged(big, 4096); err == nil {
		t.Error("read a 5000-byte token")
	}
	if b, err := sys.ReadStaged(secret, 4096); err != nil || string(b) != "not the token" {
		t.Errorf("ReadStaged(regular) = %q, %v", b, err)
	}
}

func TestWriteExclusiveReplacesAndNeverWritesThroughASymlink(t *testing.T) {
	dir := t.TempDir()
	sys := realUnix{}
	elsewhere := filepath.Join(dir, "elsewhere")
	if err := os.WriteFile(elsewhere, []byte("untouched"), 0o644); err != nil {
		t.Fatal(err)
	}
	token := filepath.Join(dir, "enroll-token")
	// A pre-existing symlink where the token goes: it is removed, not followed.
	if err := os.Symlink(elsewhere, token); err != nil {
		t.Fatal(err)
	}
	if err := sys.WriteExclusive(token, []byte("TOKEN"), 0o600); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(elsewhere); string(b) != "untouched" {
		t.Fatalf("wrote through the symlink: %q", b)
	}
	info, err := os.Lstat(token)
	if err != nil {
		t.Fatal(err)
	}
	if !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 {
		t.Fatalf("token file mode = %v, want a regular 0600 file", info.Mode())
	}
	// And over an existing regular file with a wider mode.
	if err := os.Chmod(token, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := sys.WriteExclusive(token, []byte("TOKEN2"), 0o600); err != nil {
		t.Fatal(err)
	}
	info, _ = os.Lstat(token)
	b, _ := os.ReadFile(token)
	if info.Mode().Perm() != 0o600 || string(b) != "TOKEN2" {
		t.Fatalf("after replacing: mode %v, content %q", info.Mode(), b)
	}
}

func TestCopyFileAndWriteFileReplaceWithTheGivenMode(t *testing.T) {
	dir := t.TempDir()
	sys := realUnix{}
	src := filepath.Join(dir, "agent.pkg")
	if err := os.WriteFile(src, []byte("new binary"), 0o600); err != nil {
		t.Fatal(err)
	}
	dst := filepath.Join(dir, "its-agent")
	if err := os.WriteFile(dst, []byte("old binary"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := sys.CopyFile(src, dst, 0o755); err != nil {
		t.Fatal(err)
	}
	info, _ := os.Stat(dst)
	b, _ := os.ReadFile(dst)
	if info.Mode().Perm() != 0o755 || string(b) != "new binary" {
		t.Fatalf("copied: mode %v, content %q", info.Mode(), b)
	}
	env := filepath.Join(dir, "agent.env")
	if err := sys.WriteFile(env, []byte("ITS_AGENT_ENROLL_URL=https://x\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if info, _ := os.Stat(env); info.Mode().Perm() != 0o600 {
		t.Fatalf("agent.env mode = %v", info.Mode())
	}
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.Contains(e.Name(), ".tmp-") {
			t.Errorf("a temporary file was left behind: %s", e.Name())
		}
	}
}

func TestEnsureDirTightensOnlyARootOwnedDirectory(t *testing.T) {
	dir := t.TempDir()
	sys := realUnix{}
	state := filepath.Join(dir, "state")
	if err := os.Mkdir(state, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := sys.EnsureDir(state, 0o700, true); err != nil {
		t.Fatal(err)
	}
	if info, _ := os.Stat(state); info.Mode().Perm() != 0o700 {
		t.Fatalf("state dir mode = %v, want 0700", info.Mode())
	}
	shared := filepath.Join(dir, "bin")
	if err := os.Mkdir(shared, 0o775); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(shared, 0o775); err != nil { // past the umask
		t.Fatal(err)
	}
	if err := sys.EnsureDir(shared, 0o755, false); err != nil {
		t.Fatal(err)
	}
	if info, _ := os.Stat(shared); info.Mode().Perm() != 0o775 {
		t.Fatalf("an existing shared directory was re-moded to %v", info.Mode())
	}
	fresh := filepath.Join(dir, "a", "b")
	if err := sys.EnsureDir(fresh, 0o700, true); err != nil {
		t.Fatal(err)
	}
	if info, err := os.Stat(fresh); err != nil || !info.IsDir() {
		t.Fatalf("not created: %v", err)
	}
	file := filepath.Join(dir, "file")
	_ = os.WriteFile(file, nil, 0o600)
	if err := sys.EnsureDir(file, 0o700, true); err == nil {
		t.Fatal("a file was accepted as a directory")
	}
}

func TestLstatDirReportsASymlinkAsOneAndReadsOwnerAndMode(t *testing.T) {
	dir := t.TempDir()
	sys := realUnix{}
	real := filepath.Join(dir, "toeshee")
	if err := os.Mkdir(real, 0o700); err != nil {
		t.Fatal(err)
	}
	info, err := sys.LstatDir(real)
	if err != nil || !info.IsDir || info.IsSymlink || info.Mode.Perm() != 0o700 || info.UID != os.Geteuid() {
		t.Fatalf("LstatDir(real) = %+v, %v", info, err)
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(real, link); err != nil {
		t.Fatal(err)
	}
	if info, err := sys.LstatDir(link); err != nil || !info.IsSymlink || info.IsDir {
		t.Fatalf("LstatDir(symlink) = %+v, %v; a symlink must not read as its target", info, err)
	}
}

func TestEvalSymlinksResolvesAParentLink(t *testing.T) {
	dir := t.TempDir()
	real := filepath.Join(dir, "var", "opt")
	if err := os.MkdirAll(filepath.Join(real, "toeshee"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(real, "toeshee", "agent.pkg"), nil, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(real, filepath.Join(dir, "opt")); err != nil {
		t.Fatal(err)
	}
	got, err := realUnix{}.EvalSymlinks(filepath.Join(dir, "opt", "toeshee", "agent.pkg"))
	want, _ := filepath.EvalSymlinks(filepath.Join(real, "toeshee", "agent.pkg"))
	if err != nil || got != want {
		t.Fatalf("EvalSymlinks = %q, %v; want %q", got, err, want)
	}
}

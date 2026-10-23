package update

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// Failure carries a stable reason token (spec §4.4) with the local detail.
type Failure struct {
	Token string
	Err   error
}

func (f *Failure) Error() string {
	if f.Err == nil {
		return f.Token
	}
	return f.Token + ": " + f.Err.Error()
}

func (f *Failure) Unwrap() error { return f.Err }

func fail(token string, err error) error { return &Failure{Token: token, Err: err} }

// TokenOf extracts the reason token from an error, "" if it carries none.
func TokenOf(err error) string {
	var f *Failure
	if errors.As(err, &f) {
		return f.Token
	}
	return ""
}

// SelfTestFunc runs a candidate binary's --update-selftest.
type SelfTestFunc func(ctx context.Context, binary string) error

// tempPathFor is the one name the target directory ever holds besides the
// target itself, for the length of one swap.
func tempPathFor(target string) string {
	name := ".its-agent.new"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	return filepath.Join(filepath.Dir(target), name)
}

// InstallFile is the single install primitive for the swap and the rollback
// (spec §5.1): copy beside the target, re-hash there, self-test there, then
// rename over the target. The target is never missing.
func InstallFile(ctx context.Context, src, wantSHA, target string, selftest SelfTestFunc) error {
	p, err := Prepare(ctx, src, wantSHA, target, selftest)
	if err != nil {
		return err
	}
	return p.Commit()
}

// Prepared is a verified, self-tested candidate beside its target.
type Prepared struct{ tmp, target string }

// Prepare does everything InstallFile does except the rename, so Windows can
// stop the service only once the candidate has passed.
func Prepare(ctx context.Context, src, wantSHA, target string, selftest SelfTestFunc) (*Prepared, error) {
	tmp := tempPathFor(target)
	_ = os.Remove(tmp)
	if err := copyExcl(src, tmp); err != nil {
		_ = os.Remove(tmp)
		return nil, fail(ReasonInstallFailed, err)
	}
	// Last check before the rename, at the location the file will run from.
	got, err := FileSHA256(tmp)
	if err != nil || !DigestEqual(got, wantSHA) {
		_ = os.Remove(tmp)
		return nil, fail(ReasonHashMismatch, err)
	}
	if err := selftest(ctx, tmp); err != nil {
		_ = os.Remove(tmp)
		return nil, fail(ReasonSelftestFailed, err)
	}
	return &Prepared{tmp: tmp, target: target}, nil
}

// Commit renames the candidate over the target.
func (p *Prepared) Commit() error {
	if err := replaceFile(p.tmp, p.target); err != nil {
		p.Discard()
		return fail(ReasonInstallFailed, err)
	}
	return nil
}

// Discard removes the candidate.
func (p *Prepared) Discard() { _ = os.Remove(p.tmp) }

func copyExcl(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	if err := out.Sync(); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// FileSHA256 returns the lowercase hex SHA-256 of a file.
func FileSHA256(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// DigestEqual compares two hex digests in constant time.
func DigestEqual(a, b string) bool {
	return subtle.ConstantTimeCompare([]byte(strings.ToLower(a)), []byte(strings.ToLower(b))) == 1
}

// SelfTestReport is what --update-selftest prints.
type SelfTestReport struct {
	Version string `json:"version"`
	OS      string `json:"os"`
	Arch    string `json:"arch"`
}

// SelfTestTimeout bounds one --update-selftest run (spec §5.1 step 3).
const SelfTestTimeout = 30 * time.Second

// RunSelfTest executes `binary --update-selftest` against dataDir and checks it
// reports wantVersion on this platform.
func RunSelfTest(ctx context.Context, binary, dataDir, wantVersion string) error {
	ctx, cancel := context.WithTimeout(ctx, SelfTestTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, binary, "--update-selftest")
	cmd.Env = append(os.Environ(), "ITS_AGENT_DATA_DIR="+dataDir)
	var out bytes.Buffer
	cmd.Stdout = &out
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("self-test did not exit 0: %w", err)
	}
	var r SelfTestReport
	if err := json.Unmarshal(bytes.TrimSpace(out.Bytes()), &r); err != nil {
		return fmt.Errorf("self-test output is not a report: %w", err)
	}
	want, err := ParseVersion(wantVersion)
	if err != nil {
		return err
	}
	got, err := ParseVersion(r.Version)
	if err != nil || Compare(got, want) != 0 {
		return fmt.Errorf("self-test reports version %q, want %q", r.Version, wantVersion)
	}
	if r.OS != runtime.GOOS || r.Arch != runtime.GOARCH {
		return fmt.Errorf("self-test reports %s/%s", r.OS, r.Arch)
	}
	return nil
}

// VersionCheckTimeout bounds `prev --version` (spec §4.2 step 6).
const VersionCheckTimeout = 10 * time.Second

// CheckRunsAndReports runs `binary --version` and checks it prints want.
func CheckRunsAndReports(ctx context.Context, binary, want string) error {
	ctx, cancel := context.WithTimeout(ctx, VersionCheckTimeout)
	defer cancel()
	out, err := exec.CommandContext(ctx, binary, "--version").Output()
	if err != nil {
		return err
	}
	if strings.TrimSpace(string(out)) != want {
		return fmt.Errorf("reports %q", strings.TrimSpace(string(out)))
	}
	return nil
}

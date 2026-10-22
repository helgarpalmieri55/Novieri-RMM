package update

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func writeFile(t *testing.T, path, content string) string {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o700); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte(content))
	return hex.EncodeToString(sum[:])
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func okSelfTest(context.Context, string) error { return nil }

func TestInstallFileReplacesTheTargetAndLeavesNoTempFile(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "staged", "its-agent")
	sha := writeFile(t, src, "new binary")
	target := filepath.Join(dir, "bin", BinaryName())
	writeFile(t, target, "old binary")

	var testedPath string
	err := InstallFile(t.Context(), src, sha, target, func(_ context.Context, p string) error {
		testedPath = p
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if got := readFile(t, target); got != "new binary" {
		t.Fatalf("target holds %q", got)
	}
	if filepath.Dir(testedPath) != filepath.Dir(target) {
		t.Fatalf("self-test ran %s, not a file beside the target", testedPath)
	}
	if _, err := os.Stat(tempPathFor(target)); !os.IsNotExist(err) {
		t.Fatalf("temp file left behind: %v", err)
	}
}

func TestInstallFileRefusesASourceThatNoLongerMatches(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "staged", "its-agent")
	sha := writeFile(t, src, "verified bytes")
	// Swapped after staging.
	writeFile(t, src, "attacker bytes")
	target := filepath.Join(dir, "bin", BinaryName())
	writeFile(t, target, "old binary")

	selftestRan := false
	err := InstallFile(t.Context(), src, sha, target, func(context.Context, string) error {
		selftestRan = true
		return nil
	})
	if TokenOf(err) != ReasonHashMismatch {
		t.Fatalf("err = %v, want %s", err, ReasonHashMismatch)
	}
	if selftestRan {
		t.Fatal("a mismatched file was executed for its self-test")
	}
	if got := readFile(t, target); got != "old binary" {
		t.Fatalf("target changed to %q", got)
	}
	if _, err := os.Stat(tempPathFor(target)); !os.IsNotExist(err) {
		t.Fatal("temp file left behind")
	}
}

func TestInstallFileFailedSelfTestLeavesTheTarget(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "staged", "its-agent")
	sha := writeFile(t, src, "new binary")
	target := filepath.Join(dir, "bin", BinaryName())
	writeFile(t, target, "old binary")

	err := InstallFile(t.Context(), src, sha, target, func(context.Context, string) error {
		return errors.New("exit 1")
	})
	if TokenOf(err) != ReasonSelftestFailed {
		t.Fatalf("err = %v", err)
	}
	if got := readFile(t, target); got != "old binary" {
		t.Fatalf("target changed to %q", got)
	}
}

func TestInstallFileReplacesAStaleTempFile(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "staged", "its-agent")
	sha := writeFile(t, src, "new binary")
	target := filepath.Join(dir, "bin", BinaryName())
	writeFile(t, target, "old binary")
	writeFile(t, tempPathFor(target), "left by a crash")

	if err := InstallFile(t.Context(), src, sha, target, okSelfTest); err != nil {
		t.Fatal(err)
	}
	if got := readFile(t, target); got != "new binary" {
		t.Fatalf("target holds %q", got)
	}
}

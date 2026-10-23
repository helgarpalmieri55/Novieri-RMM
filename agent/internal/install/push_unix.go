//go:build linux || darwin

package install

import (
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"syscall"
)

// RunPush is `its-agent install ...` (pushunix.go): the Linux and macOS
// install mode the SSH push runs on the target. It returns the exit code.
func RunPush(args []string, exe string) int {
	l, err := UnixLayoutFor(runtime.GOOS)
	if err != nil {
		fmt.Fprintln(os.Stderr, "its-agent install:", err)
		return 2
	}
	// Every file this creates is owner-only unless explicitly widened
	// (install.sh's `umask 077`).
	syscall.Umask(0o077)
	if err := pushInstall(realUnix{chown: true}, l, args, exe); err != nil {
		fmt.Fprintln(os.Stderr, "its-agent install:", err)
		return 1
	}
	fmt.Println("its-agent installed; the service enrolls with the control plane now")
	return 0
}

// realUnix is unixSystem over os and os/exec. chown is false only in tests
// that run unprivileged; RunPush always sets it, after the euid check.
type realUnix struct{ chown bool }

func (realUnix) Geteuid() int { return os.Geteuid() }

func (realUnix) EvalSymlinks(path string) (string, error) { return filepath.EvalSymlinks(path) }

func (realUnix) LstatDir(path string) (stagingDirInfo, error) {
	fi, err := os.Lstat(path)
	if err != nil {
		return stagingDirInfo{}, err
	}
	st, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		return stagingDirInfo{}, fmt.Errorf("%s: no owner information", path)
	}
	return stagingDirInfo{
		IsDir:     fi.IsDir(),
		IsSymlink: fi.Mode()&os.ModeSymlink != 0,
		UID:       int(st.Uid),
		Mode:      fi.Mode(),
	}, nil
}

func (realUnix) ReadStaged(path string, limit int64) ([]byte, error) {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("%s is not a regular file", path)
	}
	b, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > limit {
		return nil, fmt.Errorf("%s is larger than %d bytes", path, limit)
	}
	return b, nil
}

func (r realUnix) EnsureDir(path string, mode os.FileMode, rootOwned bool) error {
	info, err := os.Lstat(path)
	switch {
	case errors.Is(err, os.ErrNotExist):
		if err := os.MkdirAll(path, mode); err != nil {
			return err
		}
	case err != nil:
		return err
	case !info.IsDir():
		return fmt.Errorf("%s exists and is not a directory", path)
	case !rootOwned:
		return nil
	}
	if err := os.Chmod(path, mode); err != nil {
		return err
	}
	return r.own(path)
}

func (r realUnix) WriteFile(path string, data []byte, mode os.FileMode) error {
	return r.replace(path, mode, func(f *os.File) error {
		_, err := f.Write(data)
		return err
	})
}

func (r realUnix) WriteExclusive(path string, data []byte, mode os.FileMode) error {
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	// O_EXCL|O_NOFOLLOW: a file or a symlink planted between the remove and
	// the open is a failure, never followed and never written through.
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, mode)
	if err != nil {
		return err
	}
	// Mode and owner are set BEFORE a byte is written, so the token is never
	// briefly readable through a wider mode (install.sh's `: > f; chmod 0600`).
	if err := f.Chmod(mode); err != nil {
		f.Close()
		return err
	}
	if r.chown {
		if err := f.Chown(0, 0); err != nil {
			f.Close()
			return err
		}
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

func (r realUnix) CopyFile(src, dst string, mode os.FileMode) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	return r.replace(dst, mode, func(f *os.File) error {
		_, err := io.Copy(f, in)
		return err
	})
}

// replace writes a sibling temporary file and renames it over path: a running
// service keeps its old binary open, and nobody ever reads half a file.
func (r realUnix) replace(path string, mode os.FileMode, fill func(*os.File) error) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	ok := false
	defer func() {
		if !ok {
			tmp.Close()
			os.Remove(name)
		}
	}()
	if err := tmp.Chmod(mode); err != nil {
		return err
	}
	if r.chown {
		if err := tmp.Chown(0, 0); err != nil {
			return err
		}
	}
	if err := fill(tmp); err != nil {
		return err
	}
	if err := tmp.Sync(); err != nil {
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(name, path); err != nil {
		return err
	}
	ok = true
	return nil
}

func (r realUnix) own(path string) error {
	if !r.chown {
		return nil
	}
	return os.Lchown(path, 0, 0)
}

func (realUnix) Remove(path string) error { return os.Remove(path) }

func (realUnix) Run(name string, args ...string) error {
	out, err := exec.Command(name, args...).CombinedOutput()
	if err != nil {
		return fmt.Errorf("%w: %s", err, bytesTrim(out))
	}
	return nil
}

func bytesTrim(b []byte) string {
	const max = 512
	if len(b) > max {
		b = b[:max]
	}
	for len(b) > 0 && (b[len(b)-1] == '\n' || b[len(b)-1] == '\r' || b[len(b)-1] == ' ') {
		b = b[:len(b)-1]
	}
	return string(b)
}

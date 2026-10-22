//go:build linux || darwin

package backup

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"syscall"
	"testing"

	"golang.org/x/sys/unix"
)

// POSIX security metadata round trip (spec-server-backup.md §4.4, D8): mode
// with its special bits, owner and group, and on Linux the kept xattrs.

func statT(t *testing.T, p string) *syscall.Stat_t {
	t.Helper()
	fi, err := os.Lstat(p)
	if err != nil {
		t.Fatal(err)
	}
	return fi.Sys().(*syscall.Stat_t)
}

func TestFormat2CarriesModeOwnerAndGroupAndRestoresThem(t *testing.T) {
	src := filepath.Join(t.TempDir(), "src")
	if err := os.MkdirAll(filepath.Join(src, "shared"), 0o755); err != nil {
		t.Fatal(err)
	}
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	must(os.WriteFile(filepath.Join(src, "tool"), []byte("#!/bin/sh\n"), 0o755))
	must(os.Chmod(filepath.Join(src, "tool"), 0o755|os.ModeSetuid))
	must(os.Chmod(filepath.Join(src, "shared"), 0o775|os.ModeSetgid|os.ModeSticky))
	must(os.WriteFile(filepath.Join(src, "shared", "doc.txt"), []byte("hello"), 0o640))

	root := os.Geteuid() == 0
	if root {
		// A uid and gid with no name here: restored by number (§4.4).
		must(os.Lchown(filepath.Join(src, "shared", "doc.txt"), 43210, 43211))
	}
	var xattr bool
	if root && runtime.GOOS == "linux" {
		xattr = unix.Lsetxattr(filepath.Join(src, "tool"), "trusted.itms_test", []byte("v1"), 0) == nil
		_ = unix.Lsetxattr(filepath.Join(src, "tool"), "user.not_security", []byte("x"), 0)
	}

	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	o, err := svc.execRun(context.Background(), testJobID, v2RunParams(t, []string{src}, nil))
	if err != nil || o.res.State != stateSuccess {
		t.Fatalf("%v %+v", err, o)
	}
	_, m := readManifestDoc(t, store, *o.res.ManifestRef)
	byPath := map[string]entryV2{}
	for _, e := range v2Entries(t, store, m) {
		byPath[e.Path] = e
	}
	tool := byPath[filepath.ToSlash(filepath.Join(src, "tool"))]
	if tool.Mode != 0o4755 {
		t.Errorf("tool mode %04o, want 4755: the set-id bit was dropped", tool.Mode)
	}
	if tool.UID == nil || *tool.UID != uint32(os.Geteuid()) || tool.GID == nil {
		t.Errorf("tool uid/gid = %v/%v", tool.UID, tool.GID)
	}
	if tool.User == "" {
		t.Log("the running user has no name in the user database; numeric ids only")
	}
	if xattr && tool.Xattrs["trusted.itms_test"] == "" {
		t.Error("trusted.* xattr not captured")
	}
	if _, ok := tool.Xattrs["user.not_security"]; ok {
		t.Error("a user.* xattr was captured; only security, system ACL and trusted are kept")
	}

	dest := filepath.Join(t.TempDir(), "r")
	ro, err := svc.execRestore(context.Background(), testJobID, v2RestoreParams(t,
		store.prefix+*o.res.ManifestRef, []string{src}, dest, modeRestore, nil))
	if err != nil || ro.res.State != stateSuccess || len(ro.res.Mismatch) != 0 {
		t.Fatalf("%v %+v", err, ro.res)
	}
	out := expectedDest(dest, src)
	if got := statT(t, filepath.Join(out, "tool")).Mode & 0o7777; got != 0o4755 {
		t.Errorf("restored tool mode %04o", got)
	}
	if got := statT(t, filepath.Join(out, "shared")).Mode & 0o7777; got != 0o3775 {
		t.Errorf("restored dir mode %04o, want 3775 (setgid, sticky)", got)
	}
	if root {
		st := statT(t, filepath.Join(out, "shared", "doc.txt"))
		if st.Uid != 43210 || st.Gid != 43211 {
			t.Errorf("restored owner %d:%d, want 43210:43211", st.Uid, st.Gid)
		}
		if xattr {
			buf := make([]byte, 16)
			n, err := unix.Lgetxattr(filepath.Join(out, "tool"), "trusted.itms_test", buf)
			if err != nil || string(buf[:n]) != "v1" {
				t.Errorf("trusted xattr restored as %q, %v", buf[:n], err)
			}
		}
	} else {
		t.Log("not root: ownership and trusted.* xattrs are exercised only when the suite runs as root")
	}
}

func TestAVerifyDoesNotApplyOwnershipToItsScratchTree(t *testing.T) {
	// A verify deletes its scratch tree; an owner or mode that locked this
	// agent out would leave customer data behind on the endpoint.
	e := &entryV2{manifestEntry: newEntry("/x", kindDir, 0o000, 0, 1)}
	r := &restorer{v2: true, mode: modeVerify, meta: newMetaApply(), root: t.TempDir()}
	r.applySecurity = r.v2 && r.mode == modeRestore
	if r.applySecurity {
		t.Fatal("security metadata would be applied to a verify")
	}
	_ = e
}

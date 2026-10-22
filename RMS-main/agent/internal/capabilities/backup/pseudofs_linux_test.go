//go:build linux

package backup

import (
	"context"
	"io/fs"
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

// The walk never descends into a kernel pseudo filesystem, and honours
// one_filesystem, both decided where it crosses onto another device.

func TestProcIsAPseudoFilesystemAndATempDirIsNot(t *testing.T) {
	if _, err := os.Stat("/proc/self"); err != nil {
		t.Skip("/proc is not mounted here")
	}
	if !isPseudoFS("/proc") {
		t.Error("/proc not recognised; a live read of / would block on /proc/kmsg")
	}
	if isPseudoFS(t.TempDir()) {
		t.Error("a temporary directory was taken for a pseudo filesystem")
	}
}

// mountFS reports one directory as the root of another device.
type mountFS struct {
	*osTreeFS
	mount      string
	pseudoHere bool
}

type otherDev struct{ fs.FileInfo }

func (o otherDev) Sys() any {
	st := *o.FileInfo.Sys().(*syscall.Stat_t)
	st.Dev++
	return &st
}

func (m *mountFS) lstat(p string) (fs.FileInfo, error) {
	fi, err := m.osTreeFS.lstat(p)
	if err == nil && p == m.mount {
		return otherDev{fi}, nil
	}
	return fi, err
}

func (m *mountFS) pseudo(p string) bool { return m.pseudoHere && p == m.mount }

func TestAWalkSkipsPseudoMountsAndHonoursOneFilesystem(t *testing.T) {
	src := filepath.Join(t.TempDir(), "root")
	mount := filepath.Join(src, "mnt")
	if err := os.MkdirAll(mount, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(mount, "kmsg"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		name          string
		pseudo, onefs bool
		want          bool
	}{
		{"pseudo filesystem", true, false, false},
		{"one_filesystem", false, true, false},
		{"an ordinary second filesystem", false, false, true},
	} {
		t.Run(c.name, func(t *testing.T) {
			store := newFakeStore(testStorageRef)
			svc := liveService(store, &mountFS{osTreeFS: newOSTreeFS(), mount: mount, pseudoHere: c.pseudo}, mechNone)
			raw := v2RunParams(t, []string{src}, map[string]any{"workload": map[string]any{
				"id": "w", "kind": "files", "config": map[string]any{"one_filesystem": c.onefs}}})
			o, err := svc.execRun(context.Background(), testJobID, raw)
			if err != nil || o.res.State != stateSuccess {
				t.Fatalf("%v %+v", err, o)
			}
			_, got := entryFor(t, store, o, filepath.Join(mount, "kmsg"))
			if got != c.want {
				t.Errorf("file under the mount in the manifest = %v, want %v", got, c.want)
			}
		})
	}
}

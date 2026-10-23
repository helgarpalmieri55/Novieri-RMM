package backup

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"runtime/metrics"
	"strings"
	"testing"
	"time"
)

// The idle budget for the server-backup additions (spec-server-backup.md §10).
// Ordinary Tests with thresholds, named Falsifier so CI's budget step prints
// their numbers.

// ---------------------------------------------------------------------------
// B5: nothing left behind by a live-mode or format-2 run
// ---------------------------------------------------------------------------

func TestFalsifierB5_ALiveModeRunLeavesNoGoroutineOrDescriptor(t *testing.T) {
	// FALSIFYING OBSERVATION: more goroutines after the job than before it, or
	// (on Linux) a descriptor open after it that was not open before.
	src, target := liveFixture(t, 20000)
	fsys := &mutatingFS{osTreeFS: newOSTreeFS(), target: target, onOpen: func(n int, p string) {
		if n == 1 {
			appendTo(t, p, "changed once")
		}
	}}
	store := newFakeStore(testStorageRef)
	svc := liveService(store, fsys, mechNone)
	var fdsBefore map[string]bool
	if runtime.GOOS == "linux" {
		fdsBefore = openFDs(t)
	}
	runtime.GC()
	gBefore := runtime.NumGoroutine()

	o := liveRun(t, svc, src)
	if o.res.State != stateSuccess {
		t.Fatalf("%+v", o.res)
	}
	restoreAll(t, svc, store, o, src)
	runtime.GC()

	if g := runtime.NumGoroutine(); g > gBefore {
		t.Fatalf("falsifier B5: %d goroutines before, %d after a live run and restore", gBefore, g)
	}
	if fdsBefore != nil {
		if extra := newFDs(fdsBefore, openFDs(t)); len(extra) != 0 {
			t.Fatalf("falsifier B5: descriptors left open: %v", extra)
		}
	}
	t.Logf("falsifier B5: goroutines %d -> %d, descriptor growth 0 (checked on linux only: %v)",
		gBefore, runtime.NumGoroutine(), fdsBefore != nil)
}

// ---------------------------------------------------------------------------
// B7: a two-million-entry tree does not cost memory proportional to the tree
// ---------------------------------------------------------------------------

// synthFS is a tree of dirs x files empty files that exists only as answers:
// the measurement is about the engine, not about creating two million inodes.
type synthFS struct {
	root        string
	dirs, files int
}

type synthInfo struct {
	name string
	dir  bool
}

func (i synthInfo) Name() string       { return i.name }
func (i synthInfo) Size() int64        { return 0 }
func (i synthInfo) ModTime() time.Time { return time.Unix(1_700_000_000, 0) }
func (i synthInfo) IsDir() bool        { return i.dir }
func (i synthInfo) Sys() any           { return nil }
func (i synthInfo) Mode() fs.FileMode {
	if i.dir {
		return fs.ModeDir | 0o755
	}
	return 0o644
}

type emptyFile struct{ info synthInfo }

func (emptyFile) Read([]byte) (int, error)          { return 0, io.EOF }
func (emptyFile) ReadAt([]byte, int64) (int, error) { return 0, io.EOF }
func (emptyFile) Close() error                      { return nil }
func (f emptyFile) Stat() (fs.FileInfo, error)      { return f.info, nil }

func (s *synthFS) depth(p string) int {
	if p == s.root {
		return 0
	}
	return strings.Count(strings.TrimPrefix(p, s.root), string(os.PathSeparator))
}

func (s *synthFS) lstat(p string) (fs.FileInfo, error) {
	return synthInfo{name: filepath.Base(p), dir: s.depth(p) < 2}, nil
}

func (s *synthFS) readDirNames(p string) ([]string, error) {
	var n int
	var format string
	if s.depth(p) == 0 {
		n, format = s.dirs, "d%05d"
	} else {
		n, format = s.files, "file-with-a-realistic-name-%07d.docx"
	}
	out := make([]string, n)
	for i := range out {
		out[i] = fmt.Sprintf(format, i)
	}
	return out, nil
}

func (s *synthFS) open(p string) (sourceFile, error) {
	return emptyFile{info: synthInfo{name: filepath.Base(p)}}, nil
}

func (s *synthFS) readlink(string) (string, error)       { return "", os.ErrInvalid }
func (s *synthFS) capture(string, fs.FileInfo, *entryV2) {}
func (s *synthFS) pseudo(string) bool                    { return false }

// peakLiveHeap runs fn while sampling the live heap every 10 ms through
// runtime/metrics, and returns the highest sample. Only the sampler touches
// peak until it has stopped.
func peakLiveHeap(fn func()) uint64 {
	sample := []metrics.Sample{{Name: "/gc/heap/live:bytes"}}
	var peak uint64
	read := func() {
		metrics.Read(sample)
		if v := sample[0].Value.Uint64(); v > peak {
			peak = v
		}
	}
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		tick := time.NewTicker(10 * time.Millisecond)
		defer tick.Stop()
		for {
			select {
			case <-stop:
				return
			case <-tick.C:
				read()
			}
		}
	}()
	fn()
	close(stop)
	<-done
	read()
	return peak
}

func synthRun(t *testing.T, format, dirs, files int) (uint64, *runResult) {
	t.Helper()
	root := filepath.Join(string(os.PathSeparator), "synthetic")
	svc := New(Options{
		OpenStore: func(context.Context, Repository) (ObjectStore, error) { return discardStore{}, nil },
		OpenSnapshot: func(context.Context, string, []string) (Snapshot, error) {
			return passthroughSnapshot{}, nil
		},
		QueryChunks: discardDedup,
		Logger:      quiet(),
	})
	svc.treeFS = func() treeFS { return &synthFS{root: root, dirs: dirs, files: files} }
	var doc map[string]any
	_ = json.Unmarshal(runParamsJSON(t, []string{root}, nil), &doc)
	doc["format"] = format
	d := prodDedup()
	doc["dedup_params"] = map[string]any{"algo": d.Algo, "hash": d.Hash, "avg": d.Avg, "min": d.Min, "max": d.Max}
	raw, _ := json.Marshal(doc)

	runtime.GC()
	debug.FreeOSMemory()
	var res *runResult
	var err error
	peak := peakLiveHeap(func() { res, err = svc.run(context.Background(), testJobID, raw) })
	if err != nil || res.State != stateSuccess {
		t.Fatalf("%v %+v", err, res)
	}
	return peak, res
}

func TestFalsifierB7_TwoMillionEntriesDoNotCostMemoryProportionalToTheTree(t *testing.T) {
	// FALSIFYING OBSERVATION: peak live heap above 96 MiB while backing up a
	// 2,000,000-entry tree (§10 B7). The format-1 manifest holds every entry
	// in memory; the paged format-2 manifest must not.
	if raceDetector {
		t.Skip("the race detector's shadow memory is not the engine's; CI's non-race run gates this")
	}
	if testing.Short() {
		t.Skip("two million entries")
	}
	const gate = 96 << 20
	start := time.Now()
	peak, res := synthRun(t, formatV2, 2000, 1000)
	files, _ := res.DedupStats["files"].(int64)
	pages, _ := res.DedupStats["manifest_pages"].(int64)
	if files != 2000*1000 {
		t.Fatalf("the synthetic tree produced %d files", files)
	}
	t.Logf("falsifier B7: %d entries, %d manifest pages, peak live heap %.1f MiB (gate %d MiB), %s",
		files+2001, pages, float64(peak)/(1<<20), gate>>20, time.Since(start).Round(time.Millisecond))
	if peak > gate {
		t.Fatalf("falsifier B7: peak live heap %d bytes over the %d-byte gate", peak, gate)
	}

	// Not blind: the format-1 path holds the tree, and at a fifth of the size
	// the same measurement already reads several times the paged figure.
	peak1, _ := synthRun(t, formatV1, 400, 1000)
	peak2, _ := synthRun(t, formatV2, 400, 1000)
	t.Logf("falsifier B7 (not blind): 400,000 entries: format 1 peak %.1f MiB, format 2 peak %.1f MiB",
		float64(peak1)/(1<<20), float64(peak2)/(1<<20))
	if peak1 < 2*peak2 {
		t.Fatalf("the format-1 run (%d) is not clearly heavier than format 2 (%d); the measurement "+
			"cannot tell a held tree from a paged one", peak1, peak2)
	}
}

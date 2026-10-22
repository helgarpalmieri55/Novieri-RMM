package backup

import (
	"context"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// The walk: a depth-first, pre-order traversal with each directory's children
// in byte order. Entries are produced already in manifest v2 order, so nothing
// has to hold the whole tree to sort it (D6), and a directory's subtree is one
// contiguous run of entries (and so of pages).

// sourceFile is what the walk reads a regular file through. *os.File is one.
type sourceFile interface {
	io.Reader
	io.ReaderAt
	io.Closer
	Stat() (fs.FileInfo, error)
}

// treeFS is the walk's view of a filesystem. The OS implementation reads with
// backup semantics on Windows; tests substitute a synthetic tree to measure a
// two-million-entry run without creating two million files.
type treeFS interface {
	lstat(p string) (fs.FileInfo, error)
	readDirNames(p string) ([]string, error)
	open(p string) (sourceFile, error)
	readlink(p string) (string, error)
	// capture fills e's security metadata (format 2 only).
	capture(p string, fi fs.FileInfo, e *entryV2)
	// pseudo reports a kernel pseudo filesystem (proc, sysfs, ...), asked only
	// where the walk crosses onto another device.
	pseudo(p string) bool
}

// osTreeFS is the production treeFS. Its caches live for one job.
type osTreeFS struct {
	meta *metaCapture
}

func newOSTreeFS() *osTreeFS { return &osTreeFS{meta: newMetaCapture()} }

func (osTreeFS) lstat(p string) (fs.FileInfo, error) { return os.Lstat(p) }

func (osTreeFS) readDirNames(p string) ([]string, error) {
	f, err := openDirForBackup(p)
	if err != nil {
		return nil, err
	}
	defer f.Close() //nolint:errcheck
	names, err := f.Readdirnames(-1)
	if err != nil {
		return nil, err
	}
	sort.Strings(names)
	return names, nil
}

func (osTreeFS) open(p string) (sourceFile, error) { return openForBackup(p) }

func (osTreeFS) readlink(p string) (string, error) { return os.Readlink(p) }

func (o *osTreeFS) capture(p string, fi fs.FileInfo, e *entryV2) { o.meta.capture(p, fi, e) }

func (osTreeFS) pseudo(p string) bool { return isPseudoFS(p) }

// liveReadAttempts is §4.4's "re-read up to two more times".
const liveReadAttempts = 3

// walkRoot backs up one source root.
func (w *writer) walkRoot(ctx context.Context, root string) error {
	snapRoot := w.snap.Translate(root)
	fi, err := w.fs.lstat(snapRoot)
	if err != nil {
		return err
	}
	w.rootLive = w.mechanismOf(root) == mechNone
	w.rootDev, w.rootDevOK = deviceOf(fi)
	return w.visit(ctx, root, snapRoot, fi, w.rootDev)
}

func (w *writer) visit(ctx context.Context, original, snapPath string, fi fs.FileInfo, parentDev uint64) error {
	if err := ctx.Err(); err != nil {
		// The job's own deadline. Reporting a partial backup as a success is
		// not an option; the caller turns this into a failed result.
		return err
	}
	mpath := toManifestPath(original)
	if excluded(w.exclude, mpath) {
		if !fi.IsDir() {
			w.skipped++
		}
		return nil
	}
	mode := fi.Mode()
	switch {
	case mode&fs.ModeSymlink != 0:
		target, err := w.fs.readlink(snapPath)
		if err != nil {
			w.unreadable++
			w.svc.logger().Debug("backup.run: a symbolic link could not be read", "err", redact(err))
			return nil
		}
		h := w.newHeld(mpath, kindSymlink, fi, snapPath)
		t := toManifestPath(target)
		h.e.Target = &t
		w.symlinks++
		return w.complete(ctx, h)

	case fi.IsDir():
		dev, devOK := deviceOf(fi)
		if devOK && w.rootDevOK && dev != parentDev {
			// A mount point. Another filesystem is skipped when the workload
			// says one_filesystem, and a kernel pseudo filesystem always.
			if (w.oneFilesystem && dev != w.rootDev) || w.fs.pseudo(snapPath) {
				w.skipped++
				return nil
			}
		}
		h := w.newHeld(mpath, kindDir, fi, snapPath)
		w.dirs++
		if err := w.complete(ctx, h); err != nil {
			return err
		}
		names, err := w.fs.readDirNames(snapPath)
		if err != nil {
			// One unreadable directory does not fail the run: a live profile
			// always holds something locked, and aborting protects nothing.
			w.unreadable++
			w.svc.logger().Debug("backup.run: a directory could not be read", "err", redact(err))
			return nil
		}
		// A volume root's shadow path already ends in a separator, and a
		// `\\?\` path is not normalised: a doubled one would not open.
		prefix := snapPath
		if !strings.HasSuffix(prefix, string(os.PathSeparator)) {
			prefix += string(os.PathSeparator)
		}
		for _, name := range names {
			if strings.HasPrefix(name, btrfsSnapDir) || alwaysExcluded(name) {
				continue // this agent's own btrfs snapshots; Windows' own system areas
			}
			childOrig := filepath.Join(original, name)
			childSnap := prefix + name
			if r, ok := w.redirect(childOrig); ok {
				childSnap = r
			}
			cfi, err := w.fs.lstat(childSnap)
			if err != nil {
				w.unreadable++
				w.svc.logger().Debug("backup.run: an entry could not be read", "err", redact(err))
				continue
			}
			if err := w.visit(ctx, childOrig, childSnap, cfi, dev); err != nil {
				return err
			}
		}
		return nil

	case mode.IsRegular():
		err := w.file(ctx, snapPath, mpath, fi)
		if err == nil {
			return nil
		}
		var refusal *jobs.Refusal
		var se *storeError
		switch {
		case errors.As(err, &refusal), errors.As(err, &se),
			errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
			// An object-store failure is not a per-file problem: the next file
			// hits it too, and continuing would reference chunks never stored.
			return err
		}
		// One unreadable file (locked, permission-denied, Controlled Folder
		// Access) is recorded and skipped.
		w.unreadable++
		w.svc.logger().Debug("backup.run: a file could not be backed up", "err", redact(err))
		return nil

	default:
		// Devices, sockets, FIFOs: nothing meaningful to back up, and restoring
		// one would mean creating a device node as root.
		w.skipped++
		return nil
	}
}

// file chunks one regular file. STREAMING: nothing here holds a whole file.
//
// Under a live read (no snapshot) the file is stat'ed before and after; a
// change re-reads it, and one still changing after three reads is kept as last
// read and flagged. Under a snapshot one read is exact.
func (w *writer) file(ctx context.Context, snapPath, mpath string, pre fs.FileInfo) error {
	for attempt := 1; ; attempt++ {
		h, post, err := w.readFile(ctx, snapPath, mpath, pre)
		if err != nil {
			return err
		}
		if !w.rootLive || sameFile(pre, post) {
			w.files++
			return w.complete(ctx, h)
		}
		if attempt >= liveReadAttempts {
			h.e.addFlag(flagChangedDuringRead)
			w.changedDuringRead++
			w.files++
			return w.complete(ctx, h)
		}
		// Throw this attempt away; its unresolved chunks are skipped at the
		// next flush and anything already uploaded is garbage the server's
		// GC reclaims.
		w.abandon(h)
		w.release()
		if pre, err = w.fs.lstat(snapPath); err != nil {
			return err
		}
	}
}

func (w *writer) readFile(ctx context.Context, snapPath, mpath string, pre fs.FileInfo) (*heldEntry, fs.FileInfo, error) {
	f, err := w.fs.open(snapPath)
	if err != nil {
		return nil, nil, err
	}
	defer f.Close() //nolint:errcheck

	h := w.newHeld(mpath, kindFile, pre, snapPath)
	h.e.Size = 0
	h.open = true
	w.chunks.reset(f)
	// The chunker covers the file contiguously, so the running total is the
	// next chunk's offset.
	var off int64
	for {
		if err := ctx.Err(); err != nil {
			w.abandon(h)
			return nil, nil, err
		}
		chunk, err := w.chunks.next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			w.abandon(h)
			return nil, nil, err
		}
		id := chunkID(w.hasher, chunk)
		idx := len(h.e.ChunkIDs)
		h.e.ChunkIDs = append(h.e.ChunkIDs, id)
		h.e.Size += int64(len(chunk))
		w.bytesProtected += int64(len(chunk))
		chunkOff := off
		off += int64(len(chunk))
		if _, dup := w.seen[id]; dup {
			// Already resolved by this run: no upload, no place in a batch.
			w.chunksDeduped++
			w.bytesDeduped += int64(len(chunk))
			continue
		}
		// A REFERENCE, not the bytes; see dedup.go.
		if err := w.noteChunk(ctx, pendingChunk{
			id: id, path: snapPath, off: chunkOff, length: int32(len(chunk)), entry: h, idx: idx,
		}); err != nil {
			w.abandon(h)
			return nil, nil, err
		}
	}
	post, err := f.Stat()
	if err != nil {
		post = pre
	}
	w.closeEntry(h)
	return h, post, nil
}

// newHeld builds an entry and queues it behind any entry still waiting on its
// chunks, so entries leave in walk order.
func (w *writer) newHeld(mpath, kind string, fi fs.FileInfo, snapPath string) *heldEntry {
	e := entryV2{manifestEntry: newEntry(mpath, kind, uint32(fi.Mode().Perm()), 0, mtimeOf(fi.ModTime()))}
	if w.format >= formatV2 {
		e.Mode = modeBits(fi)
		w.fs.capture(snapPath, fi, &e)
	}
	h := &heldEntry{e: e, live: w.rootLive}
	w.held = append(w.held, h)
	return h
}

// complete marks an entry's walk finished and lets it go if nothing ahead of
// it is waiting.
func (w *writer) complete(ctx context.Context, h *heldEntry) error {
	w.closeEntry(h)
	w.release()
	return w.settle(ctx, false)
}

// closeEntry marks an entry's read finished; only then do its ids count
// against the held bound, so one large file being read cannot force a flush
// per chunk.
func (w *writer) closeEntry(h *heldEntry) {
	if h.open {
		h.open = false
		w.heldIDs += len(h.e.ChunkIDs)
	}
}

// abandon discards an attempt at a file that is not going into the manifest.
func (w *writer) abandon(h *heldEntry) {
	w.closeEntry(h)
	if !h.dropped {
		h.dropped = true
		w.bytesProtected -= h.e.Size
	}
}

func (w *writer) mechanismOf(root string) string {
	if d, ok := w.snap.(snapshotDetail); ok {
		if m := d.mechanism(root); m != "" {
			return m
		}
	}
	return w.defaultMech
}

func (w *writer) redirect(original string) (string, bool) {
	if r, ok := w.snap.(snapshotRedirector); ok {
		return r.redirect(original)
	}
	return "", false
}

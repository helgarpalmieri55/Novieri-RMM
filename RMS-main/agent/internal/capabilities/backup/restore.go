package backup

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"strings"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// `backup.restore`, in both `restore` and `verify` modes (spec §9).
//
// EVERY BYTE THIS FUNCTION WRITES GOES UNDER ONE ROOT. In `restore` mode that
// root is `target.path`, which the control plane supplies and an operator with
// the `backup.restore` permission chose; in `verify` mode it is a scratch
// directory this agent creates and deletes, because api.py::verify_point sends
// `target={}` and says a verify "never lands files at any caller-chosen
// destination". path.go is where that root is enforced against a hostile
// manifest.
//
// NOTHING IS REPLACED UNTIL ITS REPLACEMENT IS COMPLETE (D9). Every file is
// written to a temporary name in its destination directory, its length and
// every chunk hash verified, fsync'd, given its metadata, and only then renamed
// over the destination. A restore that fails part-way through a file leaves
// the original exactly as it was.

type restoreOutcome struct {
	job      restoreJob
	res      *restoreResult
	warnings []string
}

func (o *restoreOutcome) wire() any {
	if o.job.format < formatV2 {
		return o.res
	}
	w := o.warnings
	if w == nil {
		w = []string{}
	}
	return &restoreResultV2{restoreResult: *o.res, Warnings: w}
}

// restore is the format-1 view, which is what most of this package's tests read.
func (s *Service) restore(ctx context.Context, jobID string, raw json.RawMessage) (*restoreResult, error) {
	o, err := s.execRestore(ctx, jobID, raw)
	if err != nil {
		return nil, err
	}
	return o.res, nil
}

func (s *Service) execRestore(ctx context.Context, jobID string, raw json.RawMessage) (*restoreOutcome, error) {
	j, err := parseRestoreJob(raw)
	if err != nil {
		return nil, err
	}
	p := j.restoreParams
	key, err := decodeRepoKey(p.RepoKey)
	if err != nil {
		return nil, err
	}
	defer key.zero()
	defer releaseMemory()
	out := &restoreOutcome{job: j}

	selection := s.paths.normaliseSelection(p.Selection)
	if p.Mode == modeRestore && len(selection) == 0 {
		// An operator mistake, not an incapacity: a failed result, not a
		// refusal, and never a success over zero files.
		out.res = failedRestore("the restore selection is empty; nothing was requested")
		return out, nil
	}

	store, err := s.openStore(ctx, Repository{ID: p.RepositoryID, StorageRef: p.StorageRef, JobID: jobID})
	if err != nil {
		return nil, err
	}
	defer func() {
		if err := store.Close(); err != nil {
			s.logger().Warn("backup.restore: closing the repository transport", "err", redact(err))
		}
	}()

	blob, err := store.GetRef(ctx, p.ManifestRef)
	if err != nil {
		var refusal *jobs.Refusal
		if errors.As(err, &refusal) {
			// A standing incapacity (no read route at all) is `refused`, not a
			// `succeeded` job with a failed body; at least one module reads
			// that distinction on the wire.
			return nil, err
		}
		s.logger().Warn("backup.restore: the manifest could not be fetched", "err", redact(err))
		out.res = failedRestore("the manifest could not be fetched (%v)", redact(err))
		return out, nil
	}
	m1, m2, err := openManifestDoc(key, blob)
	if err != nil {
		return nil, err
	}

	root, cleanup, err := s.restoreRoot(p)
	if err != nil {
		return nil, err
	}
	if cleanup != nil {
		defer cleanup()
	}
	releasePriv, privWarn := acquireRestorePrivileges()
	defer releasePriv()

	r := &restorer{
		svc:       s,
		store:     store,
		key:       key,
		root:      root,
		hasher:    newBlake3(),
		plainBuf:  make([]byte, 0, defaultChunkMax),
		conflict:  j.conflict,
		stamp:     s.now().UTC().Format("20060102T150405Z"),
		v2:        m2 != nil,
		meta:      newMetaApply(),
		mode:      p.Mode,
		selection: selection,
		paths:     s.paths,
	}
	// Security metadata is applied to an operator's restore only. A verify's
	// scratch tree is deleted straight after, and an ACL that denies the
	// agent would keep it from being deleted.
	r.applySecurity = r.v2 && p.Mode == modeRestore

	if m2 != nil {
		err = r.restoreV2(ctx, m2)
	} else {
		err = r.restoreV1(ctx, m1)
	}
	if err != nil {
		var refusal *jobs.Refusal
		if errors.As(err, &refusal) {
			// Stop the whole job: a manifest that contains a traversal is
			// under someone else's control.
			return nil, err
		}
		var se *storeError
		if errors.As(err, &se) {
			s.logger().Warn("backup.restore: the repository transport failed", "err", redact(se.err))
			out.res = failedRestore("the repository could not be read (%v)", redact(se.err))
			return out, nil
		}
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			out.res = failedRestore("the job's deadline passed during the restore")
			return out, nil
		}
		// A page that would not decrypt: the manifest cannot be followed.
		s.logger().Warn("backup.restore: the manifest could not be read", "err", redact(err))
		out.res = failedRestore("the manifest could not be read (%v)", redact(err))
		return out, nil
	}

	out.warnings = appendWarn(out.warnings, privWarn...)
	out.warnings = appendWarn(out.warnings, r.meta.warnings()...)
	out.res = &restoreResult{
		State:         stateSuccess,
		RestoredBytes: r.bytes,
		// TRUE ONLY IN VERIFY MODE, and that is the server's rule: a verify
		// success stamps proven-restore evidence, an ordinary restore must not.
		Verified: p.Mode == modeVerify && len(r.mismatch) == 0,
		Mismatch: r.mismatch,
		Error:    nil,
	}
	return out, nil
}

// restoreRoot resolves the single directory everything is written under.
func (s *Service) restoreRoot(p restoreParams) (root string, cleanup func(), err error) {
	if p.Mode == modeVerify {
		dir, err := os.MkdirTemp(s.opts.ScratchParent, "its-backup-verify-")
		if err != nil {
			return "", nil, jobs.Refuse("backup.restore: no scratch directory is available for a " +
				"verify. A verify writes into a directory this agent creates and deletes; it " +
				"will not fall back to a caller-chosen destination")
		}
		return dir, func() {
			// A verify's output is evidence, not data: leaving it would double
			// every protected byte on disk somewhere no policy describes.
			if err := os.RemoveAll(dir); err != nil {
				s.logger().Warn("backup.restore: the verify scratch directory could not be "+
					"removed", "err", redact(err))
			}
		}, nil
	}

	if !filepath.IsAbs(p.Target.Path) {
		// A relative target resolves against the service's working directory,
		// on Windows commonly C:\Windows\System32.
		return "", nil, jobs.Refuse("backup.restore: target.path is not absolute. A relative " +
			"target resolves against the agent service's working directory, which no operator " +
			"chose and no console displays")
	}
	root = filepath.Clean(p.Target.Path)
	if err := os.MkdirAll(root, 0o755); err != nil {
		return "", nil, jobs.Refuse("backup.restore: the restore target could not be created")
	}
	// The root itself must not BE a symlink: everything below is checked
	// component by component, and the root is where that check starts.
	fi, err := os.Lstat(root)
	if err != nil {
		return "", nil, jobs.Refuse("backup.restore: the restore target could not be inspected")
	}
	if fi.Mode()&fs.ModeSymlink != 0 {
		return "", nil, jobs.Refuse("backup.restore: the restore target is itself a symbolic " +
			"link, so every path 'under' it is somewhere else. Nothing was written")
	}
	if !fi.IsDir() {
		return "", nil, jobs.Refuse("backup.restore: the restore target exists and is not a " +
			"directory. Nothing was written")
	}
	return root, nil, nil
}

type restorer struct {
	svc    *Service
	store  ObjectStore
	key    *repoKey
	root   string
	hasher *blake3Hasher

	// plainBuf is reused across every chunk of every file.
	plainBuf []byte

	conflict      string
	stamp         string
	v2            bool
	applySecurity bool
	meta          *metaApply
	mode          string
	selection     []string
	paths         pathRules

	bytes    int64
	mismatch []string
}

func (r *restorer) selected(p string) bool {
	return r.mode != modeRestore || selects(r.selection, r.paths.selectionKey(p))
}

// record handles one entry's failure: a refusal or a transport failure stops
// the job, anything else is recorded against its path and the restore goes on.
// An operator needs to know WHICH files did not come back.
func (r *restorer) record(e *entryV2, err error) error {
	var refusal *jobs.Refusal
	var se *storeError
	if errors.As(err, &refusal) || errors.As(err, &se) ||
		errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return err
	}
	r.mismatch = append(r.mismatch, e.Path)
	r.svc.logger().Warn("backup.restore: an entry was not restored", "err", redact(err))
	return nil
}

// ---------------------------------------------------------------------------
// Format 1: one document, every entry in memory
// ---------------------------------------------------------------------------

func (r *restorer) restoreV1(ctx context.Context, m *backupManifest) error {
	entries := filetreeEntries(m)
	sortEntries(entries)

	// Announce the fetch order so the store can ask for grants a window at a
	// time. THIS LOOP'S CONDITIONS MUST MIRROR r.entry / r.file / r.stream,
	// CLAUSE FOR CLAUSE: announce an id the walk never fetches and a real one
	// is evicted from the window; fetch one never announced and it costs its
	// own round trip. The filesystem-dependent gates in r.file are NOT
	// mirrored and must not be: they read state that does not exist yet, and
	// they can only over-announce, never under-announce.
	if br, ok := r.store.(BatchReader); ok {
		br.Expect(announceOrder(entries, r))
	}

	// TWO PASSES, AND THE ORDER IS A SECURITY PROPERTY: symbolic links last.
	// `data -> /etc` followed by `data/passwd` cannot escape, because `data` is
	// already a real directory when the file is written and the link then
	// fails to be created. assertParentNotSymlink is the second guard.
	for pass := 0; pass < 2; pass++ {
		for i := range entries {
			if err := ctx.Err(); err != nil {
				return err
			}
			e := entryV2{manifestEntry: entries[i]}
			if (e.Kind == kindSymlink) != (pass == 1) {
				continue
			}
			if !r.selected(e.Path) {
				continue
			}
			if err := r.entry(ctx, &e); err != nil {
				if err := r.record(&e, err); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

// announceOrder predicts the chunk fetch order of entries, distinct suffixes
// only. Positive gate on kindFile: nothing else ever reaches r.stream, and a
// forged manifest can put chunk_ids on a dir entry.
func announceOrder(entries []manifestEntry, r *restorer) []string {
	seen := make(map[string]struct{}, len(entries))
	order := make([]string, 0, len(entries))
	for i := range entries {
		e := &entries[i]
		if e.Kind != kindFile {
			continue
		}
		// r.entry's first act: an entry relUnderRoot rejects is abandoned
		// before r.file, so its chunks are never fetched. Pure; safe to repeat.
		if _, err := r.paths.relUnderRoot(e.Path); err != nil {
			continue
		}
		if !r.selected(e.Path) {
			continue
		}
		for _, id := range e.ChunkIDs {
			if !validChunkID(id) {
				// r.stream aborts the whole entry on the first invalid id.
				break
			}
			suffix := chunkSuffix(id)
			if _, dup := seen[suffix]; dup {
				continue
			}
			seen[suffix] = struct{}{}
			order = append(order, suffix)
		}
	}
	return order
}

// ---------------------------------------------------------------------------
// Format 2: paged, streamed; nothing holds the tree
// ---------------------------------------------------------------------------

func (r *restorer) restoreV2(ctx context.Context, m *manifestV2) error {
	br, _ := r.store.(BatchReader)
	for pass := 0; pass < 2; pass++ {
		// Pass 1 finalizes directories (mode, owner, ACL, mtime) once their
		// whole subtree is written: entries are in walk order, so a directory
		// is finished exactly when the walk leaves it, and the stack of open
		// directories is never deeper than the tree.
		var open []*entryV2
		finalize := func(upTo string) {
			for len(open) > 0 {
				top := open[len(open)-1]
				if upTo != "" && strings.HasPrefix(upTo, top.Path+"/") {
					return
				}
				open = open[:len(open)-1]
				r.finishDir(top)
			}
		}
		for _, st := range m.Streams {
			if st.Type != streamTypeFiletree {
				continue // a stream this agent does not restore; skipped, as in v1
			}
			for _, pid := range st.Pages {
				if err := ctx.Err(); err != nil {
					return err
				}
				if br != nil {
					br.Expect([]string{chunkSuffix(pid)})
				}
				data, err := fetchPage(ctx, r.store, r.key, r.hasher, pid)
				if err != nil {
					return err
				}
				entries, err := parseEntryPage(data)
				if err != nil {
					return err
				}
				if pass == 0 && br != nil {
					br.Expect(announceV2(entries, r))
				}
				for i := range entries {
					if err := ctx.Err(); err != nil {
						return err
					}
					e := &entries[i]
					if pass == 1 {
						finalize(e.Path)
					}
					if !r.selected(e.Path) {
						continue
					}
					switch {
					case pass == 0 && e.Kind != kindSymlink:
						err = r.entry(ctx, e)
						if len(e.ChunkPages) > 0 && br != nil {
							br.Expect(announceV2(entries[i+1:], r))
						}
					case pass == 1 && e.Kind == kindSymlink:
						err = r.entry(ctx, e)
					case pass == 1 && e.Kind == kindDir && r.mode == modeRestore:
						cp := *e
						open = append(open, &cp)
					}
					if err != nil {
						if err := r.record(e, err); err != nil {
							return err
						}
						err = nil
					}
				}
			}
		}
		finalize("")
	}
	return nil
}

func announceV2(entries []entryV2, r *restorer) []string {
	flat := make([]manifestEntry, 0, len(entries))
	for i := range entries {
		if len(entries[i].ChunkPages) > 0 {
			break // its ids arrive with its own pages; announced then
		}
		flat = append(flat, entries[i].manifestEntry)
	}
	return announceOrder(flat, r)
}

// finishDir applies a restored directory's own metadata after its contents.
func (r *restorer) finishDir(e *entryV2) {
	rel, err := r.paths.relUnderRoot(e.Path)
	if err != nil {
		return
	}
	dest, err := safeJoin(r.root, rel)
	if err != nil {
		return
	}
	fi, err := os.Lstat(dest)
	if err != nil || !fi.IsDir() {
		return
	}
	t := mtimeTo(e.Mtime)
	_ = os.Chtimes(dest, t, t)
	if r.applySecurity {
		applyMeta(r.meta, dest, e)
	}
}

// ---------------------------------------------------------------------------
// One entry
// ---------------------------------------------------------------------------

func (r *restorer) entry(ctx context.Context, e *entryV2) error {
	rel, err := r.paths.relUnderRoot(e.Path)
	if err != nil {
		return err
	}
	switch e.Kind {
	case kindDir:
		return ensureDirUnderRoot(r.root, rel, fs.FileMode(e.Mode).Perm()|0o700)
	case kindFile:
		return r.file(ctx, e, rel)
	case kindSymlink:
		return r.symlink(e, rel)
	default:
		// Skipped, not refused: an old agent that refused the whole restore
		// over one unknown kind could not restore the entries it understands.
		return errUnsafeName
	}
}

func (r *restorer) fileMode(e *entryV2) fs.FileMode {
	if r.v2 {
		return goMode(e.Mode)
	}
	return fs.FileMode(e.Mode).Perm()
}

func (r *restorer) file(ctx context.Context, e *entryV2, rel string) error {
	dir := path.Dir(rel)
	if dir != "." {
		if err := ensureDirUnderRoot(r.root, dir, 0o755); err != nil {
			return err
		}
	}
	if err := assertParentNotSymlink(r.root, rel); err != nil {
		return err
	}
	dest, err := safeJoin(r.root, rel)
	if err != nil {
		return err
	}
	fi, statErr := os.Lstat(dest)
	exists := statErr == nil
	if exists {
		// A destination that is ALREADY a symbolic link: something put it
		// there and this agent did not. Refused rather than replaced.
		if fi.Mode()&fs.ModeSymlink != 0 {
			return jobs.Refuse("backup.restore: a restore destination already exists as a symbolic " +
				"link. Writing through it would write outside the restore root. Nothing further was " +
				"written")
		}
		switch r.conflict {
		case conflictSkip:
			return nil
		case conflictRename:
			if dest, err = r.freeName(dest); err != nil {
				return err
			}
			exists = false
		default:
			if fi.IsDir() {
				return errors.New("backup.restore: the destination is a directory")
			}
		}
	}

	tmp, err := os.CreateTemp(filepath.Dir(dest), ".itms-restore-*")
	if err != nil {
		// The Controlled Folder Access case, among others.
		return err
	}
	tmpName := tmp.Name()
	written, err := r.stream(ctx, tmp, e)
	if err == nil && written != e.Size {
		err = errors.New("backup.restore: a restored file's length does not match its manifest entry")
	}
	if err == nil {
		// Durable before it replaces anything.
		err = tmp.Sync()
	}
	if cerr := tmp.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		_ = os.Remove(tmpName)
		return err
	}

	// Metadata goes on the temporary file, so the file never appears under its
	// real name with the wrong owner, mode or ACL. Failures are counted, not
	// fatal: the bytes are what a restore owes first. The owner and ACL go on
	// LAST: a restored ACL may deny this process the attribute writes above it.
	if err := os.Chmod(tmpName, r.fileMode(e)); err != nil {
		r.svc.logger().Debug("backup.restore: permissions could not be set", "err", redact(err))
	}
	t := mtimeTo(e.Mtime)
	if err := os.Chtimes(tmpName, t, t); err != nil {
		r.svc.logger().Debug("backup.restore: mtime could not be set", "err", redact(err))
	}
	if r.applySecurity {
		applyMeta(r.meta, tmpName, e)
	}
	if err := replaceFile(tmpName, dest, exists); err != nil {
		_ = os.Remove(tmpName)
		return err
	}
	r.bytes += written
	return nil
}

// freeName is `rename` conflict mode: the restored copy lands beside the
// existing file as `<name>.restored-<time>`.
func (r *restorer) freeName(dest string) (string, error) {
	base := dest + ".restored-" + r.stamp
	for n := 1; n <= 100; n++ {
		cand := base
		if n > 1 {
			cand = fmt.Sprintf("%s-%d", base, n)
		}
		if _, err := os.Lstat(cand); os.IsNotExist(err) {
			return cand, nil
		}
	}
	return "", errors.New("backup.restore: no free name for a renamed restore")
}

// replaceFile renames tmp over dest in one step. Windows refuses to replace a
// read-only file, so that attribute is cleared and the rename tried once more.
func replaceFile(tmp, dest string, existed bool) error {
	err := os.Rename(tmp, dest)
	if err != nil && existed {
		if cerr := os.Chmod(dest, 0o600); cerr == nil {
			err = os.Rename(tmp, dest)
		}
	}
	return err
}

// stream fetches, decrypts, VERIFIES and writes one file's chunks, one at a
// time. The id check is not a verify-mode extra: it is the only thing that
// distinguishes "the repository returned my chunk" from "a chunk".
func (r *restorer) stream(ctx context.Context, f *os.File, e *entryV2) (int64, error) {
	var total int64
	write := func(ids []string) error {
		for _, id := range ids {
			if err := ctx.Err(); err != nil {
				return err
			}
			if !validChunkID(id) {
				return errors.New("backup.restore: a manifest entry names a chunk id that is " +
					"not 64 hex characters")
			}
			blob, err := r.store.Get(ctx, chunkSuffix(id))
			if err != nil {
				return &storeError{err: err}
			}
			plain, err := r.key.open(r.plainBuf[:0], blob)
			if err != nil {
				return err
			}
			r.plainBuf = plain
			if got := chunkID(r.hasher, plain); got != id {
				return errors.New("backup.restore: a chunk's contents do not hash to the id " +
					"the manifest names for it")
			}
			n, err := f.Write(plain)
			total += int64(n)
			if err != nil {
				return err
			}
		}
		return nil
	}
	if len(e.ChunkPages) == 0 {
		return total, write(e.ChunkIDs)
	}
	br, _ := r.store.(BatchReader)
	for _, pid := range e.ChunkPages {
		data, err := fetchPage(ctx, r.store, r.key, r.hasher, pid)
		if err != nil {
			return total, err
		}
		ids, err := parseIDPage(data)
		if err != nil {
			return total, err
		}
		if br != nil {
			order := make([]string, 0, len(ids))
			for _, id := range ids {
				if !validChunkID(id) {
					break
				}
				order = append(order, chunkSuffix(id))
			}
			br.Expect(order)
		}
		if err := write(ids); err != nil {
			return total, err
		}
	}
	return total, nil
}

func (r *restorer) symlink(e *entryV2, rel string) error {
	if e.Target == nil || *e.Target == "" {
		return errUnsafeName
	}
	dir := path.Dir(rel)
	if dir != "." {
		if err := ensureDirUnderRoot(r.root, dir, 0o755); err != nil {
			return err
		}
	}
	if err := assertParentNotSymlink(r.root, rel); err != nil {
		return err
	}
	dest, err := safeJoin(r.root, rel)
	if err != nil {
		return err
	}
	if _, err := os.Lstat(dest); err == nil {
		switch r.conflict {
		case conflictSkip:
			return nil
		case conflictRename:
			if dest, err = r.freeName(dest); err != nil {
				return err
			}
		}
	}
	// The link's TARGET is not validated against the root, deliberately: a
	// symlink is a string, creating one writes nothing outside the root, and
	// this agent never FOLLOWS one (links are created last, every parent is
	// Lstat-checked). Rewriting a customer's link would change their data.
	// No metadata is applied to a link: on Windows an ACL set by name lands
	// on the link's target, which may be anywhere.
	return os.Symlink(filepath.FromSlash(*e.Target), dest)
}

// validChunkID rejects anything that is not 64 lowercase hex characters BEFORE
// it becomes a path component. `chunks/{id[:2]}/{id}` with an unvalidated id is
// an object-store key an editor of the manifest would choose.
func validChunkID(id string) bool {
	if len(id) != 64 {
		return false
	}
	buf, err := hex.DecodeString(id)
	if err != nil || len(buf) != 32 {
		return false
	}
	return bytes.Equal([]byte(id), []byte(hex.EncodeToString(buf)))
}

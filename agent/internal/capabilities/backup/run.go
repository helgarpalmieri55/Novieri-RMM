package backup

import (
	"context"
	"encoding/json"
	"errors"
	"runtime"

	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// `backup.run`: snapshot -> walk -> chunk -> dedup -> encrypt -> store ->
// manifest.

// runOutcome is one run's result before it is shaped for the wire: format 1
// sends the pinned BackupRunResult, format 2 adds §5.3's members.
type runOutcome struct {
	job      runJob
	res      *runResult
	cons     *consistencyDoc
	warnings []string
}

func (o *runOutcome) wire() any {
	if o.job.format < formatV2 {
		return o.res
	}
	w := o.warnings
	if w == nil {
		w = []string{}
	}
	return &runResultV2{runResult: *o.res, WorkloadID: o.job.workloadID, Consistency: o.cons, Warnings: w}
}

// run is the format-1 view, which is what most of this package's tests read.
func (s *Service) run(ctx context.Context, jobID string, raw json.RawMessage) (*runResult, error) {
	o, err := s.execRun(ctx, jobID, raw)
	if err != nil {
		return nil, err
	}
	return o.res, nil
}

// execRun is the whole capability. It returns a REFUSAL (a jobs.Refusal error)
// for anything this agent will not act on, and a result with `state="failed"`
// for anything it tried and could not finish — see Run's comment for why an
// operational failure must not be a Go error.
func (s *Service) execRun(ctx context.Context, jobID string, raw json.RawMessage) (*runOutcome, error) {
	// Everything that can be checked before anything is opened, is.
	j, err := parseRunJob(raw, s.now())
	if err != nil {
		return nil, err
	}
	roots, err := j.Sources.roots()
	if err != nil {
		return nil, err
	}
	key, err := decodeRepoKey(j.RepoKey)
	if err != nil {
		return nil, err
	}
	query, err := s.queryChunks()
	if err != nil {
		return nil, err
	}
	// Deferred FIRST, so it runs on every path out, including a recovered
	// panic. The key does not outlive the job (invariant #3).
	defer key.zero()
	defer releaseMemory()
	out := &runOutcome{job: j}
	failed := func(cons *consistencyDoc, format string, args ...any) (*runOutcome, error) {
		out.res, out.cons = failedRun(format, args...), cons
		return out, nil
	}

	// SeBackupPrivilege and SeSecurityPrivilege, for this job only (D10).
	releasePriv, privWarn := acquireBackupPrivileges()
	defer releasePriv()
	out.warnings = appendWarn(out.warnings, privWarn...)

	sctx := withSnapOptions(ctx, snapOptions{vssType: j.vssType, allowLive: j.allowLive,
		failOnWriter: j.failOnWriter})
	snap, err := s.openSnapshot(sctx, j.Snapshot, roots)
	if err != nil {
		var refusal *jobs.Refusal
		if errors.As(err, &refusal) {
			// A STANDING incapacity: §9's `refused` column.
			return nil, err
		}
		// Anything else is operational and becomes a failed RESULT, so
		// `ingest_backup_result` runs. The message names no path; providers
		// identify a root by its index into the trimmed, de-duplicated and
		// sorted source list, which is not the policy's own ordering.
		s.logger().Warn("backup.run: the volume snapshot could not be taken", "err", redact(err))
		var wf *writerFailure
		var cons *consistencyDoc
		if errors.As(err, &wf) {
			cons = &consistencyDoc{Level: levelSnapshot, CrashConsistent: false, Snapshot: mechVSS,
				Writers: wf.writers, Databases: []databaseDoc{}}
		}
		return failed(cons, "the volume snapshot could not be taken (%v). Any root index above counts "+
			"into the source paths after they are trimmed, de-duplicated and sorted, which is not "+
			"the policy's own ordering", redact(err))
	}
	released := false
	release := func() error {
		if released {
			return nil
		}
		released = true
		return snap.Release()
	}
	defer func() {
		if err := release(); err != nil {
			s.logger().Warn("backup.run: releasing the volume snapshot", "err", redact(err))
		}
	}()

	store, err := s.openStore(ctx, Repository{ID: j.RepositoryID, StorageRef: j.StorageRef, JobID: jobID})
	if err != nil {
		return nil, err
	}
	defer func() {
		if err := store.Close(); err != nil {
			s.logger().Warn("backup.run: closing the repository transport", "err", redact(err))
		}
	}()

	w := s.newWriter(j, store, key, snap)
	w.dedup = newDedupSession(w, jobID, query, j.DedupParams.Max)
	defer w.dedup.closeFile()

	for _, root := range roots {
		if err := w.walkRoot(ctx, root); err != nil {
			var refusal *jobs.Refusal
			if errors.As(err, &refusal) {
				return nil, err
			}
			var se *storeError
			if errors.As(err, &se) {
				s.logger().Warn("backup.run: the repository could not be reached", "err", redact(err))
				return failed(nil, "the repository could not be reached (%v)", redact(err))
			}
			// A root that cannot be walked at all is a failed run, not a
			// refusal. The message names no path: `error` reaches reports.
			s.logger().Warn("backup.run: a source root could not be walked", "err", redact(err))
			return failed(nil, "a source root could not be read (%v)", redact(err))
		}
	}
	grant, err := w.finish(ctx)
	if err != nil {
		s.logger().Warn("backup.run: the closing dedup exchange failed", "err", redact(err))
		return failed(nil, "the repository could not be reached (%v)", redact(err))
	}

	// Every read is done. Release now, not at return: on VSS this is
	// BackupComplete and the third writer check, whose answer belongs in this
	// result and in the manifest header.
	if err := release(); err != nil {
		s.logger().Warn("backup.run: releasing the volume snapshot", "err", redact(err))
		out.warnings = appendWarn(out.warnings, warnReleaseFailed)
	}
	cons, warns := w.consistency(roots)
	out.cons = cons
	out.warnings = appendWarn(out.warnings, warns...)

	blob, err := w.sealManifest(key, cons)
	if err != nil {
		s.logger().Warn("backup.run: the manifest could not be sealed", "err", err)
		return failed(cons, "the manifest could not be sealed (%v)", err)
	}
	name, err := s.newName()
	if err != nil {
		s.logger().Warn("backup.run: the manifest name could not be minted", "err", err)
		return failed(cons, "the manifest name could not be generated (%v)", err)
	}
	// spec §5 names this object `{storage_ref}/manifests/{backup_point_id}`.
	// The point does not exist until the server ingests this result, so the
	// agent mints its own 128-bit name.
	ref, err := store.Put(ctx, grant, "manifests/"+name, blob)
	if err != nil {
		// ObjectStore.Put never returns a *jobs.Refusal, so folding every Put
		// error into a failed result is safe.
		s.logger().Warn("backup.run: the manifest could not be stored", "err", redact(err))
		return failed(cons, "the manifest could not be stored (%v)", redact(err))
	}
	if ref == "" {
		return failed(cons, "the repository transport returned an empty manifest reference")
	}

	stats := map[string]any{
		"chunks_uploaded":  w.chunksUploaded,
		"chunks_deduped":   w.chunksDeduped,
		"bytes_deduped":    w.bytesDeduped,
		"files":            w.files,
		"dirs":             w.dirs,
		"symlinks":         w.symlinks,
		"skipped_entries":  w.skipped,
		"unreadable_files": w.unreadable,
	}
	if j.format >= formatV2 {
		stats["manifest_pages"] = w.pagesUploaded
		stats["manifest_bytes"] = w.pageBytesStored
		stats["files_changed_during_read"] = w.changedDuringRead
	}
	out.res = &runResult{
		State:          stateSuccess,
		ManifestRef:    &ref,
		BytesProtected: w.bytesProtected,
		BytesStored:    w.bytesStored,
		ChunkCount:     int64(len(w.seen)),
		DedupStats:     stats,
		Error:          nil,
	}
	return out, nil
}

// heldEntry is one manifest entry that has been walked but not yet written,
// because a chunk it references is still waiting on the dedup answer (a live
// read may still rewrite it) or because it is still being read.
type heldEntry struct {
	e          entryV2
	open       bool
	dropped    bool
	live       bool
	unresolved int
}

// Bounds on what waits between flushes. With the batch bound they cap the
// memory a run holds for entries, however large the tree.
const (
	maxHeldEntries  = 8192
	maxHeldChunkIDs = 1 << 16
)

// writer holds one run's state. Everything expensive in it is allocated once
// per JOB and released with the job.
type writer struct {
	svc           *Service
	store         ObjectStore
	key           *repoKey
	snap          Snapshot
	fs            treeFS
	exclude       []string
	oneFilesystem bool
	format        int
	defaultMech   string

	hasher *blake3Hasher
	chunks *chunker
	dedup  *dedupSession
	// cipherBuf is reused across every chunk of every file.
	cipherBuf []byte

	// seen holds the chunk ids this run has resolved (uploaded, or held by the
	// repository). A later reference to one needs neither an upload nor a
	// place in a batch. Ids only, never contents.
	seen map[string]struct{}

	held     []*heldEntry
	heldIDs  int
	entries  []manifestEntry // format 1: the whole list, sorted at the end
	pager    *pageWriter     // format 2: pages, streamed
	pagesOut []pendingChunk
	pagerErr error

	rootLive  bool
	rootDev   uint64
	rootDevOK bool

	bytesProtected    int64
	bytesStored       int64
	bytesDeduped      int64
	chunksUploaded    int64
	chunksDeduped     int64
	files             int64
	dirs              int64
	symlinks          int64
	skipped           int64
	unreadable        int64
	changedDuringRead int64
	pagesUploaded     int64
	pageBytesStored   int64
}

func (s *Service) newWriter(j runJob, store ObjectStore, key *repoKey, snap Snapshot) *writer {
	w := &writer{
		svc:           s,
		store:         store,
		key:           key,
		snap:          snap,
		fs:            s.newTreeFS(),
		exclude:       j.Sources.Exclude,
		oneFilesystem: j.oneFilesystem,
		format:        j.format,
		defaultMech:   j.Snapshot,
		hasher:        newBlake3(),
		chunks:        newChunker(j.DedupParams),
		seen:          make(map[string]struct{}),
		// Sized ONCE, to the largest ciphertext a chunk can produce; growing on
		// demand measured as 14.8 MB of cumulative allocation over a 64 MiB
		// file where the buffers are 8 MB (falsifier 3).
		cipherBuf: make([]byte, 0, j.DedupParams.Max+nonceLen+tagLen),
	}
	if w.defaultMech == snapshotAuto {
		w.defaultMech = mechNone
	}
	if j.format >= formatV2 {
		w.pager = newPageWriter(j.DedupParams.Max, w.hasher, func(id string, data []byte) {
			w.pagesOut = append(w.pagesOut, pendingChunk{id: id, data: data})
		})
	} else {
		w.entries = make([]manifestEntry, 0, 256)
	}
	return w
}

func (w *writer) liveRef(p *pendingChunk) bool { return p.entry != nil && p.entry.live }

// drop removes an entry whose file could not be captured consistently.
func (w *writer) drop(h *heldEntry) {
	if h.dropped {
		return
	}
	h.dropped = true
	w.bytesProtected -= h.e.Size
	if h.e.Kind == kindFile && !h.open {
		w.files--
	}
	w.unreadable++
}

// noteChunk queues one reference, flushing first if the batch is full.
func (w *writer) noteChunk(ctx context.Context, p pendingChunk) error {
	if len(w.dedup.pending) >= maxChunkBatch {
		if err := w.settle(ctx, false); err != nil {
			return err
		}
	}
	w.dedup.add(p)
	return nil
}

// release writes out, in walk order, every entry at the front of the queue that
// is finished and fully resolved.
func (w *writer) release() {
	i := 0
	for ; i < len(w.held); i++ {
		h := w.held[i]
		if h.open || h.unresolved > 0 {
			break
		}
		w.heldIDs -= len(h.e.ChunkIDs)
		if !h.dropped {
			w.emit(h)
		}
		w.held[i] = nil
	}
	if i > 0 {
		n := copy(w.held, w.held[i:])
		for k := n; k < len(w.held); k++ {
			w.held[k] = nil
		}
		w.held = w.held[:n]
	}
}

func (w *writer) emit(h *heldEntry) {
	if w.pager == nil {
		w.entries = append(w.entries, h.e.manifestEntry)
		return
	}
	if err := w.pager.add(&h.e); err != nil && w.pagerErr == nil {
		w.pagerErr = err
	}
}

// settle keeps every bound: the batch, the page bytes waiting on it, and the
// entries held behind it. force drains everything (the end of the walk).
func (w *writer) settle(ctx context.Context, force bool) error {
	for {
		if w.pagerErr != nil {
			return w.pagerErr
		}
		moved := 0
		for moved < len(w.pagesOut) && len(w.dedup.pending) < maxChunkBatch {
			w.dedup.add(w.pagesOut[moved])
			w.pagesOut[moved] = pendingChunk{}
			moved++
		}
		if moved > 0 {
			n := copy(w.pagesOut, w.pagesOut[moved:])
			w.pagesOut = w.pagesOut[:n]
		}
		full := len(w.dedup.pending) >= maxChunkBatch ||
			w.dedup.pendingData >= maxPendingPageBytes ||
			len(w.held) >= maxHeldEntries ||
			w.heldIDs >= maxHeldChunkIDs ||
			len(w.pagesOut) > 0
		if !full && (!force || !(len(w.dedup.pending) > 0 || w.releasable())) {
			return nil
		}
		if err := w.dedup.flush(ctx); err != nil {
			return err
		}
		w.release()
	}
}

// releasable reports whether anything held is waiting only on a flush.
func (w *writer) releasable() bool {
	for _, h := range w.held {
		if h.open {
			return false
		}
		if h.unresolved > 0 {
			return true
		}
	}
	return false
}

// finish resolves everything, closes the manifest's last page and returns a
// grant live now for the manifest write. Two exchanges at minimum, as ever: the
// last batch, then a closing one that reports its uploads.
func (w *writer) finish(ctx context.Context) (wire.UploadGrant, error) {
	if err := w.dedup.flush(ctx); err != nil {
		return wire.UploadGrant{}, err
	}
	w.release()
	if err := w.settle(ctx, true); err != nil {
		return wire.UploadGrant{}, err
	}
	if w.pager != nil {
		w.pager.cut()
		if err := w.settle(ctx, true); err != nil {
			return wire.UploadGrant{}, err
		}
	}
	return w.dedup.grantNow(ctx)
}

// consistency is §5.3's document for this run, and the warnings it implies.
func (w *writer) consistency(roots []string) (*consistencyDoc, []string) {
	c := &consistencyDoc{
		Level:                  levelSnapshot,
		CrashConsistent:        true,
		Writers:                []writerDoc{},
		FilesChangedDuringRead: w.changedDuringRead,
		Databases:              []databaseDoc{},
	}
	var warns []string
	for i, root := range roots {
		m := w.mechanismOf(root)
		if i == 0 {
			c.Snapshot = m
		}
		if m == mechNone {
			c.Level, c.CrashConsistent, c.Snapshot = levelLive, false, mechNone
		}
	}
	if c.Level == levelLive {
		warns = append(warns, warnNotSnapshotConsistent)
	}
	if w.changedDuringRead > 0 {
		warns = append(warns, warnChangedDuringRead)
	}
	if r, ok := w.snap.(writerReporter); ok {
		docs, ww := r.writerReport()
		if docs != nil {
			c.Writers = docs
		}
		warns = append(warns, ww...)
	}
	if mw, ok := w.fs.(interface{ warnings() []string }); ok {
		warns = append(warns, mw.warnings()...)
	}
	return c, warns
}

// sealManifest builds the manifest this run's format calls for and seals it.
func (w *writer) sealManifest(key *repoKey, cons *consistencyDoc) ([]byte, error) {
	if w.pager == nil {
		sortEntries(w.entries)
		return sealManifest(key, &backupManifest{
			SchemaVersion: manifestSchemaVersion,
			Streams:       []filetreeStream{{Type: streamTypeFiletree, Entries: w.entries}},
		})
	}
	m := &manifestV2{
		SchemaVersion: manifestSchemaV2,
		Header: manifestHeaderV2{
			WorkloadKind: workloadFiles,
			Consistency:  cons,
			HostOS:       runtime.GOOS,
			PageIndex:    w.pager.index,
		},
		Streams: []streamV2{{
			Type:       streamTypeFiletree,
			Name:       "files",
			EntryCount: w.pager.entryCount,
			Pages:      w.pager.pages,
		}},
	}
	if m.Header.PageIndex == nil {
		m.Header.PageIndex = []pageIndexDoc{}
	}
	if m.Streams[0].Pages == nil {
		m.Streams[0].Pages = []string{}
	}
	return sealJSON(key, m)
}

func appendWarn(dst []string, codes ...string) []string {
	for _, c := range codes {
		dup := false
		for _, d := range dst {
			if d == c {
				dup = true
				break
			}
		}
		if !dup {
			dst = append(dst, c)
		}
	}
	return dst
}

// chunkSuffix is spec §5's layout: `chunks/{id[:2]}/{id}`. The two-character
// fan-out keeps any one prefix listing tractable during the control plane's
// mark-and-sweep GC (§8).
func chunkSuffix(id string) string {
	return "chunks/" + id[:2] + "/" + id
}

// storeError marks a failure of the repository transport, as distinct from a
// failure to read one file. The two are handled completely differently: a bad
// file is skipped, a bad store aborts the run.
type storeError struct{ err error }

func (e *storeError) Error() string { return "repository transport: " + e.err.Error() }
func (e *storeError) Unwrap() error { return e.err }

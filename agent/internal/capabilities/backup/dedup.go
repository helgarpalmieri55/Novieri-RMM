package backup

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strconv"

	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// The dedup exchange (docs/modules/backup/spec.md §5.2,
// docs/kernel/wire-protocol/BackupChunkQueryMsg.schema.json).
//
// # Why the check moved off this endpoint
//
// `ObjectStore.Has` used to answer "does the repository already hold this
// chunk?" once per chunk, against a bucket this agent has no credential for and
// must never be given (invariant #3, and backup.go's `openStore` refusal). The
// question does not need endpoint storage access at all: the CONTROL PLANE
// knows which chunks a repository holds, and it can answer for a whole batch
// at once and hand back the write grant for exactly the answers that were
// "no" — which is what removes both the credential and the per-chunk round
// trip.
//
// # The batch bound, and why it is a count of REFERENCES and not of bytes
//
// A run produces six figures of chunks. Neither the hash list nor the answer
// may be unbounded, so both sides cap a batch at maxChunkBatch — the same
// number as `app/gateway/protocol.py::MAX_CHUNK_BATCH`, pinned to the exported
// schema's `maxItems` by schema_test.go rather than to a comment.
//
// What a batch HOLDS is the load-bearing part. Buffering the chunk PLAINTEXT
// until the answer arrives would be the obvious implementation and it would
// make allocation scale with the data backed up — which budget_test.go's
// falsifier 3 exists to catch and would catch. So a pending entry is a
// REFERENCE: an id, the path it came from, an offset and a length, about 100
// bytes. A full batch is ~100 KB regardless of chunk size, and a six-figure
// hash list is never one allocation because it is never one batch.
//
// The cost is that a chunk the server says is NEW is read a second time, from
// the snapshot, to be sealed and uploaded. That is a real cost — a first-ever
// backup reads its data twice — and it buys a round-trip count that scales
// with 1/1024 of the chunk count instead of 1:1, plus a footprint that does not
// scale with anything. A DEDUPLICATING backup re-reads almost nothing, because
// only new chunks are re-read.
//
// # The re-read is verified, and that is not paranoia
//
// A chunk id is BLAKE3 of its plaintext and the repository is content
// addressed, so uploading different bytes under an id another device already
// references would corrupt every backup point that shares that chunk. Between
// the first read and the second the bytes cannot change UNDER A REAL SNAPSHOT
// — but `Options.OpenSnapshot` is a seam, `passthroughSnapshot` reads the live
// filesystem, and a torn re-read there would silently poison the repository.
// So the second read is re-hashed and the run FAILS on a mismatch.

// maxChunkBatch bounds one query and one answer. Pinned to
// BackupChunkQueryMsg.schema.json's `maxItems` by schema_test.go: a server that
// tightens the bound and an agent that does not would send batches the control
// plane drops at the parse layer, and the run would hang on an answer that
// never comes.
const maxChunkBatch = 1024

// QueryChunks is the transport seam for one exchange: send a wire.ChunkQuery
// over the agent's authenticated connection and return the wire.ChunkGrant
// that comes back.
//
// It is an INJECTED SEAM because the transport lives outside this package, and
// a capability that dialled anything itself would be a second path out of the
// endpoint. internal/chunkreply implements it.
type QueryChunks func(ctx context.Context, q wire.ChunkQuery) (wire.ChunkGrant, error)

// pendingChunk is one chunk awaiting an answer. A REFERENCE, never bytes —
// see the package note above. `path` is the SNAPSHOT path, so a re-read comes
// from the same point-in-time view the first read did.
type pendingChunk struct {
	id     string
	path   string
	off    int64
	length int32
	// entry and idx locate the reference in its held manifest entry, so a live
	// read that changed under us can be rewritten there. Nil for a page.
	entry *heldEntry
	idx   int
	// data carries a manifest page's bytes. Pages exist only in memory, so
	// they cannot be referenced; settle() bounds how many wait here.
	data []byte
}

// dedupSession drives the exchange for one run.
//
// It holds at most maxChunkBatch references, one reusable read buffer, at most
// ONE open file, and at most maxPendingPageBytes of manifest pages. Nothing in
// it scales with the tree, the file count or the file size.
type dedupSession struct {
	w     *writer
	jobID string
	query QueryChunks

	pending     []pendingChunk
	pendingData int
	// stored carries the ids uploaded since the LAST query, reported on the
	// next one so the control plane can index them. Bounded by maxChunkBatch
	// because a flush uploads at most one per reference.
	stored    []string
	grant     wire.UploadGrant
	haveGrant bool
	seq       int

	// readBuf is re-used across every re-read, sized once to the largest chunk
	// the repository's parameters permit.
	readBuf []byte

	// One open file, reused while consecutive pending entries share a path.
	// Closed before the flush returns, so falsifier 1b reads zero descriptors.
	openPath string
	openFile sourceFile
}

func newDedupSession(w *writer, jobID string, q QueryChunks, maxChunk int) *dedupSession {
	return &dedupSession{
		w:       w,
		jobID:   jobID,
		query:   q,
		pending: make([]pendingChunk, 0, maxChunkBatch),
		stored:  make([]string, 0, maxChunkBatch),
		readBuf: make([]byte, maxChunk),
	}
}

// add queues one reference. The caller (writer.settle) keeps the batch bound.
func (d *dedupSession) add(p pendingChunk) {
	if p.entry != nil {
		p.entry.unresolved++
	}
	d.pendingData += len(p.data)
	d.pending = append(d.pending, p)
}

// flush asks the control plane which pending chunks are new, uploads exactly
// those, and reports the previous batch's uploads.
//
// A flush with nothing pending and nothing to report still runs: it is how the
// manifest write gets a live grant, and how the last batch's uploads are
// indexed.
func (d *dedupSession) flush(ctx context.Context) error {
	check := make([]string, 0, len(d.pending))
	asked := make(map[string]struct{}, len(d.pending))
	for _, p := range d.pending {
		if p.entry != nil && p.entry.dropped {
			continue
		}
		if _, dup := asked[p.id]; dup {
			continue
		}
		asked[p.id] = struct{}{}
		check = append(check, p.id)
	}
	d.seq++
	q := wire.ChunkQuery{
		JobID: d.jobID,
		// Correlates one answer with one question on a connection carrying
		// everything else. Derived from a counter, so it leaks nothing.
		QueryID: "q" + strconv.Itoa(d.seq),
		Check:   check,
		Stored:  d.stored,
	}
	grant, err := d.query(ctx, q)
	if err != nil {
		return &storeError{err: err}
	}
	if grant.QueryID != q.QueryID || grant.JobID != d.jobID {
		// An answer to a different question, or for a different job. Continuing
		// would apply one batch's dedup verdict to another batch's chunks,
		// which silently drops uploads.
		return &storeError{err: fmt.Errorf(
			"the control plane answered query %q for job %q; this run asked %q for %q",
			clean(grant.QueryID), clean(grant.JobID), q.QueryID, clean(d.jobID))}
	}

	isNew := make(map[string]struct{}, len(grant.New))
	for _, id := range grant.New {
		if _, ok := asked[id]; !ok {
			// The server named a chunk this batch did not ask about. There is no
			// safe reading of that: uploading it would mean writing an object
			// whose bytes this run does not have.
			return &storeError{err: fmt.Errorf(
				"the control plane named chunk %s, which this batch did not ask about",
				clean(id))}
		}
		isNew[id] = struct{}{}
	}
	d.grant, d.haveGrant = grant.Upload, true
	d.stored = d.stored[:0]

	defer d.closeFile()
	uploaded := make(map[string]struct{}, len(isNew))
	for i := range d.pending {
		p := &d.pending[i]
		if p.entry != nil {
			p.entry.unresolved--
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		if p.entry != nil && p.entry.dropped {
			continue
		}
		_, fresh := isNew[p.id]
		_, done := uploaded[p.id]
		if !fresh || done {
			// First-writer-wins (spec §5): the repository (or this batch)
			// already holds this chunk, so it is referenced and not re-uploaded.
			if p.data == nil {
				d.w.seen[p.id] = struct{}{}
				d.w.chunksDeduped++
				d.w.bytesDeduped += int64(p.length)
			}
			continue
		}
		if err := d.upload(ctx, p, uploaded); err != nil {
			return err
		}
	}
	d.pending = d.pending[:0]
	d.pendingData = 0
	return nil
}

// errChunkMoved is a re-read that no longer hashes to the id already recorded.
var errChunkMoved = errors.New("a chunk changed between being hashed and being read for upload; " +
	"refusing to store different bytes under a content id")

// upload re-reads one chunk from the snapshot, verifies it still hashes to the
// id the manifest already carries, seals it and writes it under the grant.
//
// Under a live read a mismatch is not corruption, it is the file being
// written: the bytes are stored under THEIR id and the entry is rewritten to
// match, flagged changed_during_read. Under a snapshot it cannot happen, and
// uploading anyway would poison every point that shares the id, so the run
// fails.
func (d *dedupSession) upload(ctx context.Context, p *pendingChunk, uploaded map[string]struct{}) error {
	buf := p.data
	id := p.id
	if buf == nil {
		f, err := d.fileFor(p.path)
		if err != nil {
			if d.w.liveRef(p) {
				d.w.drop(p.entry)
				return nil
			}
			return &storeError{err: err}
		}
		buf = d.readBuf[:p.length]
		n, err := f.ReadAt(buf, p.off)
		if err != nil && !(errors.Is(err, io.EOF) && n == len(buf)) {
			if d.w.liveRef(p) {
				// Truncated since it was read: no consistent copy exists this
				// run. The next run picks it up.
				d.w.drop(p.entry)
				return nil
			}
			return &storeError{err: err}
		}
		if got := chunkID(d.w.hasher, buf); got != p.id {
			if !d.w.liveRef(p) {
				return &storeError{err: errChunkMoved}
			}
			p.entry.e.ChunkIDs[p.idx] = got
			if !p.entry.e.hasFlag(flagChangedDuringRead) {
				p.entry.e.addFlag(flagChangedDuringRead)
				d.w.changedDuringRead++
			}
			id = got
			if _, ok := d.w.seen[id]; ok {
				return nil
			}
			if _, ok := uploaded[id]; ok {
				return nil
			}
		}
	}
	sealed, err := d.w.key.seal(d.w.cipherBuf[:0], buf)
	if err != nil {
		return err
	}
	d.w.cipherBuf = sealed
	if d.grant.MaxBytes > 0 && int64(len(sealed)) > d.grant.MaxBytes {
		// The grant's `content-length-range` is signed into the policy and S3
		// would reject this anyway; catching it here turns a mid-run 400 into a
		// legible failure and saves the round trip.
		return &storeError{err: fmt.Errorf(
			"a sealed chunk is %d bytes and the write grant permits %d",
			len(sealed), d.grant.MaxBytes)}
	}
	if _, err := d.w.store.Put(ctx, d.grant, chunkSuffix(id), sealed); err != nil {
		return &storeError{err: err}
	}
	uploaded[id] = struct{}{}
	d.stored = append(d.stored, id)
	if p.data != nil {
		d.w.pagesUploaded++
		d.w.pageBytesStored += int64(len(sealed))
		return nil
	}
	d.w.seen[id] = struct{}{}
	d.w.chunksUploaded++
	d.w.bytesStored += int64(len(sealed))
	return nil
}

func (d *dedupSession) fileFor(path string) (sourceFile, error) {
	if d.openFile != nil && d.openPath == path {
		return d.openFile, nil
	}
	d.closeFile()
	f, err := d.w.fs.open(path)
	if err != nil {
		return nil, err
	}
	d.openFile, d.openPath = f, path
	return f, nil
}

func (d *dedupSession) closeFile() {
	if d.openFile != nil {
		if err := d.openFile.Close(); err != nil {
			d.w.svc.logger().Debug("backup.run: closing a re-read source", "err", redact(err))
		}
		d.openFile, d.openPath = nil, ""
	}
}

// grantNow runs the closing exchange: nothing pending, it REPORTS the last
// batch's uploads so the control plane indexes them (otherwise the next run
// re-uploads every one) and returns a grant that is live now, rather than one
// issued before however long the walk took.
func (d *dedupSession) grantNow(ctx context.Context) (wire.UploadGrant, error) {
	if err := d.flush(ctx); err != nil {
		return wire.UploadGrant{}, err
	}
	if !d.haveGrant {
		return wire.UploadGrant{}, &storeError{err: fmt.Errorf(
			"no write grant: the control plane answered no dedup query for this run")}
	}
	return d.grant, nil
}

// queryChunks resolves the exchange seam, or refuses.
//
// The refusal is the same shape as `openStore`'s and for the same reason: a
// build with no way to ask the control plane cannot back anything up, and that
// is a standing incapacity of the deployment rather than a transient error.
func (s *Service) queryChunks() (QueryChunks, error) {
	if s.opts.QueryChunks == nil {
		return nil, jobs.Refuse("backup: this agent has no way to ask the control plane which " +
			"chunks a repository already holds. The dedup check and the write grant travel " +
			"together on one `backup_chunk_query`/`backup_chunk_grant` exchange over the agent's " +
			"authenticated connection (docs/kernel/wire-protocol/BackupChunkQueryMsg.schema.json); " +
			"without it there is no grant, and this agent holds no object-store credential of its " +
			"own and will not be given one")
	}
	return s.opts.QueryChunks, nil
}

package backup

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// Manifest v2 paging (spec-server-backup.md §5.2, D6).
//
// Entries are serialised as NDJSON into pages, and a page is an ordinary chunk:
// content-addressed, sealed under the repository key, deduplicated through the
// same exchange as file data. Nothing holds the tree.
//
// Page boundaries fall on entry boundaries and are content-defined: a page is
// cut after an entry whose line hashes to zero modulo pageCutModulus, once the
// page is at least a quarter of the largest object. An unchanged directory
// therefore yields the same pages day to day, and the manifest itself dedups.
//
// A file whose chunk id list would crowd a page moves the list into id pages
// (newline-separated ids) named by the entry's `chunk_pages`, so no object
// exceeds the repository's largest chunk however large the file.

const (
	pageCutModulus = 64
	// maxPendingPageBytes bounds manifest pages waiting for a dedup answer.
	maxPendingPageBytes = 16 << 20
	chunkIDLineLen      = 65 // 64 hex + '\n'
)

type pageWriter struct {
	max, min  int
	maxInline int
	idsPer    int
	hasher    *blake3Hasher
	out       func(id string, data []byte)

	buf         []byte
	n           int
	first, last string

	pages      []string
	index      []pageIndexDoc
	entryCount int64
}

func newPageWriter(maxObject int, h *blake3Hasher, out func(string, []byte)) *pageWriter {
	idsPer := maxObject / chunkIDLineLen
	if idsPer < 1 {
		idsPer = 1
	}
	inline := maxObject / 4 / (chunkIDLineLen + 2)
	if inline < 1 {
		inline = 1
	}
	return &pageWriter{
		max:       maxObject,
		min:       maxObject / 4,
		maxInline: inline,
		idsPer:    idsPer,
		hasher:    h,
		out:       out,
		buf:       make([]byte, 0, maxObject),
	}
}

// add appends one entry. It may emit id pages and a full entry page through
// out, in that order, so an entry's id pages always precede the page that
// names them.
func (pw *pageWriter) add(e *entryV2) error {
	if len(e.ChunkIDs) > pw.maxInline {
		for i := 0; i < len(e.ChunkIDs); i += pw.idsPer {
			end := i + pw.idsPer
			if end > len(e.ChunkIDs) {
				end = len(e.ChunkIDs)
			}
			data := make([]byte, 0, (end-i)*chunkIDLineLen)
			for _, id := range e.ChunkIDs[i:end] {
				data = append(data, id...)
				data = append(data, '\n')
			}
			id := chunkID(pw.hasher, data)
			pw.out(id, data)
			e.ChunkPages = append(e.ChunkPages, id)
		}
		e.ChunkIDs = []string{}
	}
	line, err := json.Marshal(e)
	if err != nil {
		return fmt.Errorf("a manifest entry is not encodable: %w", err)
	}
	line = append(line, '\n')
	if len(line) > pw.max {
		return fmt.Errorf("a manifest entry is %d bytes, larger than the repository's largest "+
			"object (%d)", len(line), pw.max)
	}
	if len(pw.buf)+len(line) > pw.max {
		pw.cut()
	}
	if pw.n == 0 {
		pw.first = e.Path
	}
	pw.buf = append(pw.buf, line...)
	pw.last = e.Path
	pw.n++
	pw.entryCount++
	if len(pw.buf) >= pw.min && lineHash(line)%pageCutModulus == 0 {
		pw.cut()
	}
	return nil
}

func (pw *pageWriter) cut() {
	if pw.n == 0 {
		return
	}
	data := make([]byte, len(pw.buf))
	copy(data, pw.buf)
	id := chunkID(pw.hasher, data)
	pw.out(id, data)
	pw.index = append(pw.index, pageIndexDoc{
		Stream: 0, Page: len(pw.pages), First: pw.first, Last: pw.last, Entries: pw.n,
	})
	pw.pages = append(pw.pages, id)
	pw.buf = pw.buf[:0]
	pw.n = 0
}

// lineHash is FNV-1a, inlined so the cut decision allocates nothing.
func lineHash(b []byte) uint64 {
	h := uint64(14695981039346656037)
	for _, c := range b {
		h ^= uint64(c)
		h *= 1099511628211
	}
	return h
}

// ---------------------------------------------------------------------------
// Reading
// ---------------------------------------------------------------------------

// maxPageBlob bounds what one page fetch may decrypt: a page is a chunk.
const maxPageBlob = hardChunkMax + nonceLen + tagLen

// fetchPage reads, decrypts and VERIFIES one page. The id check is what makes
// a page an object this manifest actually named, not one it was swapped for.
func fetchPage(ctx context.Context, store ObjectStore, key *repoKey, h *blake3Hasher, id string) ([]byte, error) {
	if !validChunkID(id) {
		return nil, jobs.Refuse("backup.restore: a manifest names a page id that is not 64 hex characters")
	}
	blob, err := store.Get(ctx, chunkSuffix(id))
	if err != nil {
		return nil, &storeError{err: err}
	}
	if len(blob) > maxPageBlob {
		return nil, jobs.Refuse("backup.restore: a manifest page is larger than any chunk can be")
	}
	plain, err := key.open(nil, blob)
	if err != nil {
		return nil, err
	}
	if chunkID(h, plain) != id {
		return nil, jobs.Refuse("backup.restore: a manifest page does not hash to the id the " +
			"manifest names for it")
	}
	return plain, nil
}

// parseEntryPage decodes one NDJSON page. The parse error is not included: it
// quotes decrypted customer metadata.
func parseEntryPage(data []byte) ([]entryV2, error) {
	out := make([]entryV2, 0, bytes.Count(data, []byte{'\n'}))
	for len(data) > 0 {
		i := bytes.IndexByte(data, '\n')
		line := data
		if i >= 0 {
			line, data = data[:i], data[i+1:]
		} else {
			data = nil
		}
		if len(line) == 0 {
			continue
		}
		var e entryV2
		if err := json.Unmarshal(line, &e); err != nil {
			return nil, jobs.Refuse("backup.restore: a manifest page did not decode to entries")
		}
		if e.ChunkIDs == nil {
			e.ChunkIDs = []string{}
		}
		out = append(out, e)
	}
	return out, nil
}

var errBadIDPage = errors.New("backup.restore: a chunk id page is malformed")

func parseIDPage(data []byte) ([]string, error) {
	if len(data)%chunkIDLineLen != 0 {
		return nil, errBadIDPage
	}
	out := make([]string, 0, len(data)/chunkIDLineLen)
	for i := 0; i < len(data); i += chunkIDLineLen {
		if data[i+64] != '\n' {
			return nil, errBadIDPage
		}
		out = append(out, string(data[i:i+64]))
	}
	return out, nil
}

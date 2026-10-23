package backup

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// Test apparatus: a fake repository, a fixture tree built to be hard, and the
// two job-params builders.

// ---------------------------------------------------------------------------
// A fake object store
// ---------------------------------------------------------------------------

// fakeStore is an in-memory repository. It is deliberately strict about the
// things the real one would be: it refuses to overwrite an existing object
// (first-writer-wins, spec §5) unless told otherwise, and it counts every
// operation so a test can assert that dedup actually skipped an upload rather
// than merely reporting that it had.
type fakeStore struct {
	mu      sync.Mutex
	objects map[string][]byte
	prefix  string

	puts, gets, refGets int
	// prefixKey is `{storage_ref}/`, the value a legitimate write grant names.
	prefixKey  string
	grantsUsed []string
	// chunkGets counts Gets against `chunks/` suffixes only, so a test can
	// assert that a forged chunk id never became an object-store key without
	// the manifest fetch confusing the count.
	chunkGets int

	// failPut/failGet let a test inject a transport failure at a chosen object.
	failPut func(suffix string) error
	failGet func(suffix string) error
	// closed records that the capability released the transport.
	closed bool
	// overwritten records a Put to a suffix that already existed, which
	// first-writer-wins says must never happen for a chunk.
	overwritten []string
}

// testGrant is the grant a test uses when it seeds an object directly, so
// seeding goes through the same authorized path a real write does.
func testGrant() wire.UploadGrant {
	return wire.UploadGrant{
		URL:       "https://s3.test/bucket",
		Fields:    map[string]string{"policy": "x"},
		KeyPrefix: testStorageRef + "/",
		MaxBytes:  64 << 20,
	}
}

func newFakeStore(storageRef string) *fakeStore {
	return &fakeStore{
		objects:   map[string][]byte{},
		prefix:    "s3://test-bucket/" + storageRef + "/",
		prefixKey: storageRef + "/",
	}
}

func (f *fakeStore) Put(_ context.Context, grant wire.UploadGrant, suffix string, data []byte) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failPut != nil {
		if err := f.failPut(suffix); err != nil {
			return "", err
		}
	}
	// EVERY write carries a grant, and the grant's prefix must be this
	// repository's. A capability that wrote without one, or under a grant for
	// somewhere else, is the whole failure this exchange exists to prevent — so
	// the fake refuses rather than recording it.
	if grant.URL == "" || grant.KeyPrefix != f.prefixKey {
		return "", fmt.Errorf("write with grant %q for prefix %q; this repository is %q",
			grant.URL, grant.KeyPrefix, f.prefixKey)
	}
	f.grantsUsed = append(f.grantsUsed, grant.URL)
	f.puts++
	if _, exists := f.objects[suffix]; exists && strings.HasPrefix(suffix, "chunks/") {
		f.overwritten = append(f.overwritten, suffix)
	}
	f.objects[suffix] = bytes.Clone(data)
	// The SUFFIX, not a full reference -- mirrors internal/backupstore.Put's
	// real contract (`return suffix, nil`). The agent does not know the
	// bucket and must not report one (backup.go's ObjectStore.Put doc); a
	// fake that returned `f.prefix + suffix` here would let a test believe a
	// full ref is what an agent reports, which is exactly the shape
	// `test_an_absolute_ref_from_an_older_agent_is_refused` (backend) proves
	// the control plane now refuses. Callers that need the full reference a
	// real restore actually receives (`BackupPoint.manifest_ref`, server-
	// derived from this suffix) build it explicitly via `f.prefix + suffix`,
	// the same convention `dispatch_test.go` already uses.
	return suffix, nil
}

func (f *fakeStore) Get(_ context.Context, suffix string) ([]byte, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failGet != nil {
		if err := f.failGet(suffix); err != nil {
			return nil, err
		}
	}
	f.gets++
	if strings.HasPrefix(suffix, "chunks/") {
		f.chunkGets++
	}
	b, ok := f.objects[suffix]
	if !ok {
		return nil, fmt.Errorf("no such object")
	}
	return bytes.Clone(b), nil
}

// GetRef takes the FULL reference a real restore genuinely receives
// (`BackupPoint.manifest_ref`, server-derived from the suffix a prior `Put`
// reported -- see backup.go's ObjectStore.GetRef doc), unlike `Put`'s return
// value, which is a bare suffix. A caller driving backup.run then
// backup.restore against this same fake in one process must build that full
// reference itself (`store.prefix + suffix`, `dispatch_test.go`'s
// convention) rather than feed Put's return value straight back in here --
// that shortcut is exactly the contradiction this fake used to encode.
func (f *fakeStore) GetRef(ctx context.Context, ref string) ([]byte, error) {
	f.mu.Lock()
	f.refGets++
	prefix := f.prefix
	f.mu.Unlock()
	if !strings.HasPrefix(ref, prefix) {
		return nil, fmt.Errorf("reference outside this repository")
	}
	return f.Get(ctx, strings.TrimPrefix(ref, prefix))
}

func (f *fakeStore) Close() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.closed = true
	return nil
}

func (f *fakeStore) chunkCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for k := range f.objects {
		if strings.HasPrefix(k, "chunks/") {
			n++
		}
	}
	return n
}

func (f *fakeStore) blobs() [][]byte {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([][]byte, 0, len(f.objects))
	for _, v := range f.objects {
		out = append(out, v)
	}
	return out
}

// ---------------------------------------------------------------------------
// Service wiring
// ---------------------------------------------------------------------------

const testStorageRef = "11111111-1111-1111-1111-111111111111/backup/22222222-2222-2222-2222-222222222222"

// testKey is a fixed 32-byte AES key. NOT a secret and never used anywhere but
// here; the point is that the test can decrypt what the engine wrote and prove
// it was encrypted at all.
var testKey = func() string {
	k := make([]byte, aesKeyLen)
	for i := range k {
		k[i] = byte(i * 7)
	}
	return base64.StdEncoding.EncodeToString(k)
}()

// testDedup keeps chunks small so a fixture can straddle every boundary
// without a multi-megabyte file per case. min is hardChunkMin, so this is also
// the smallest configuration the engine accepts.
func testDedup() dedupSpec {
	return dedupSpec{Algo: defaultDedupAlgo, Hash: defaultDedupHash, Avg: 1024, Min: 512, Max: 4096}
}

// quiet returns a logger that discards, and its record log for the tests that
// assert on what was NOT written.
type capture struct {
	mu      sync.Mutex
	records []string
}

func (c *capture) Enabled(context.Context, slog.Level) bool { return true }

func (c *capture) Handle(_ context.Context, r slog.Record) error {
	var b strings.Builder
	b.WriteString(r.Message)
	r.Attrs(func(a slog.Attr) bool {
		b.WriteString(" ")
		b.WriteString(a.Key)
		b.WriteString("=")
		b.WriteString(fmt.Sprint(a.Value.Any()))
		return true
	})
	c.mu.Lock()
	c.records = append(c.records, b.String())
	c.mu.Unlock()
	return nil
}

func (c *capture) WithAttrs([]slog.Attr) slog.Handler { return c }
func (c *capture) WithGroup(string) slog.Handler      { return c }

func (c *capture) all() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return strings.Join(c.records, "\n")
}

func quiet() *slog.Logger {
	return slog.New(slog.NewTextHandler(discardWriter{}, &slog.HandlerOptions{Level: slog.LevelError + 1}))
}

type discardWriter struct{}

func (discardWriter) Write(p []byte) (int, error) { return len(p), nil }

// newTestService wires a Service against a fake store and a passthrough
// snapshot. The snapshot seam is what makes the whole engine testable on a
// machine with no VSS and no APFS.
func newTestService(store *fakeStore, log *slog.Logger) *Service {
	svc, _ := newTestServiceWithDedup(store, log)
	return svc
}

// newTestServiceWithDedup returns the service AND the fake control plane, for
// the tests that assert on the exchange itself.
func newTestServiceWithDedup(store *fakeStore, log *slog.Logger) (*Service, *fakeDedup) {
	dedup := newFakeDedup(testStorageRef)
	return New(Options{
		OpenStore: func(context.Context, Repository) (ObjectStore, error) { return store, nil },
		OpenSnapshot: func(context.Context, string, []string) (Snapshot, error) {
			return passthroughSnapshot{}, nil
		},
		QueryChunks: dedup.answer,
		Logger:      log,
	}), dedup
}

func runParamsJSON(t *testing.T, roots []string, exclude []string) json.RawMessage {
	t.Helper()
	doc := map[string]any{
		"repository_id": "22222222-2222-2222-2222-222222222222",
		"storage_ref":   testStorageRef,
		"repo_key":      testKey,
		"snapshot":      "vss",
		"sources":       map[string]any{"paths": roots, "exclude": exclude},
		"dedup_params": map[string]any{
			"algo": defaultDedupAlgo, "hash": defaultDedupHash,
			"avg": testDedup().Avg, "min": testDedup().Min, "max": testDedup().Max,
		},
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func restoreParamsJSON(t *testing.T, manifestRef string, selection []string, targetPath, mode string) json.RawMessage {
	t.Helper()
	target := map[string]any{}
	if targetPath != "" {
		target["path"] = targetPath
	}
	doc := map[string]any{
		"repository_id": "22222222-2222-2222-2222-222222222222",
		"storage_ref":   testStorageRef,
		"manifest_ref":  manifestRef,
		"selection":     selection,
		"target":        target,
		"mode":          mode,
		"repo_key":      testKey,
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// ---------------------------------------------------------------------------
// The fixture tree
// ---------------------------------------------------------------------------

// fixtureFile is one file in the tree, with the reason it is there. Every entry
// exists to make some code path produce a DIFFERENT value from its neighbours —
// this project's defining defect is a fixture where the guarded and unguarded
// paths agree, and a round trip over three identical small text files is
// exactly that fixture.
type fixtureFile struct {
	rel  string
	size int    // -1 means "use literal"
	lit  string // literal content
	mode fs.FileMode
	why  string
}

// fixtureFiles straddles every boundary the chunker has, at the test dedup
// parameters (min 512, avg 1024, max 4096).
func fixtureFiles() []fixtureFile {
	return []fixtureFile{
		{rel: "empty.txt", size: 0, mode: 0o644,
			why: "zero chunks, not one empty chunk: the classic off-by-one"},
		{rel: "one-byte.bin", size: 1, mode: 0o644,
			why: "a chunk far below min, so the boundary rule never fires"},
		{rel: "no-trailing-newline.txt", size: -1, lit: "the last line has no newline",
			mode: 0o600, why: "text handling must not append or strip anything"},
		{rel: "under-min-511.bin", size: 511, mode: 0o644, why: "one byte below min"},
		{rel: "exactly-min-512.bin", size: 512, mode: 0o644, why: "exactly min"},
		{rel: "exactly-avg-1024.bin", size: 1024, mode: 0o644, why: "exactly avg"},
		{rel: "under-max-4095.bin", size: 4095, mode: 0o644, why: "one byte below max"},
		{rel: "exactly-max-4096.bin", size: 4096, mode: 0o755,
			why: "exactly max: the forced-boundary path, and a non-default mode"},
		{rel: "over-max-4097.bin", size: 4097, mode: 0o644,
			why: "one byte over max: forces a second chunk of exactly 1 byte"},
		{rel: "large-100k.bin", size: 100_000, mode: 0o644,
			why: "many content-defined boundaries, and the buffer refill path"},
		{rel: "duplicate-of-large.bin", size: 100_000, mode: 0o644,
			why: "byte-identical to large-100k.bin: every chunk must dedup"},
		{rel: "naïve-café-日本語.txt", size: -1, lit: "unicode: naïve café 日本語 — ok\n",
			mode: 0o644, why: "a non-ASCII filename AND non-ASCII contents"},
		{rel: "a/b/c/d/e/f/g/h/i/deep.txt", size: -1, lit: "nine levels down\n", mode: 0o644,
			why: "a deep path: every component must be created, in order"},
		{rel: "spaces and 'quotes'.txt", size: -1, lit: "awkward name\n", mode: 0o644,
			why: "a name that would break anything built by string concatenation"},
	}
}

// fill writes deterministic pseudo-random bytes. Pseudo-random rather than
// repetitive because a rolling hash over a repeating pattern emits boundaries
// at pathologically regular intervals, which is the one input shape that would
// make the chunker look correct while being wrong.
func fill(b []byte, seed uint64) {
	x := seed
	for i := range b {
		x += 0x9E3779B97F4A7C15
		z := x
		z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9
		z = (z ^ (z >> 27)) * 0x94D049BB133111EB
		z ^= z >> 31
		b[i] = byte(z >> 24)
	}
}

// buildFixture materialises the tree and returns its root.
func buildFixture(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	src := filepath.Join(root, "src")
	if err := os.MkdirAll(src, 0o755); err != nil {
		t.Fatal(err)
	}

	for _, f := range fixtureFiles() {
		p := filepath.Join(src, filepath.FromSlash(f.rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		var content []byte
		if f.size >= 0 {
			content = make([]byte, f.size)
			// duplicate-of-large shares large-100k's seed on purpose.
			seed := uint64(len(f.rel))
			if strings.HasSuffix(f.rel, "large-100k.bin") || f.rel == "duplicate-of-large.bin" {
				seed = 0xD00D
			}
			fill(content, seed)
		} else {
			content = []byte(f.lit)
		}
		if err := os.WriteFile(p, content, f.mode); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(p, f.mode); err != nil {
			t.Fatal(err)
		}
	}

	// An EMPTY DIRECTORY. It has no files under it, so a manifest that recorded
	// only files would restore a tree that silently lost it.
	if err := os.Mkdir(filepath.Join(src, "empty-dir"), 0o750); err != nil {
		t.Fatal(err)
	}
	// A RELATIVE symlink and an ABSOLUTE one. Restore must reproduce the link
	// itself, not the file it points at, and must not follow either.
	if err := os.Symlink("empty.txt", filepath.Join(src, "rel-link")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(src, "one-byte.bin"), filepath.Join(src, "abs-link")); err != nil {
		t.Fatal(err)
	}
	// A DANGLING symlink: os.Stat on it fails, so any code that stats instead
	// of lstats loses it.
	if err := os.Symlink("nowhere-at-all", filepath.Join(src, "dangling-link")); err != nil {
		t.Fatal(err)
	}
	return src
}

// expectedDest states the source-path -> restored-path rule INDEPENDENTLY of
// the implementation: a drive `C:` becomes a directory `C`, the leading
// separator goes, and the rest joins under the root. If this helper called
// relUnderRoot the round trip would be comparing the code to itself.
func expectedDest(restoreRoot, srcAbs string) string {
	s := filepath.ToSlash(srcAbs)
	if vol := filepath.VolumeName(srcAbs); len(vol) == 2 && vol[1] == ':' {
		s = s[:1] + s[2:]
	}
	return filepath.Join(restoreRoot, filepath.FromSlash(strings.TrimPrefix(s, "/")))
}

// walkTree lists a tree as rel-path -> lstat info, with contents for files and
// targets for symlinks.
type node struct {
	kind    string
	mode    fs.FileMode
	content []byte
	target  string
}

func walkTree(t *testing.T, root string) map[string]node {
	t.Helper()
	out := map[string]node{}
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, p)
		if err != nil {
			return err
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		n := node{mode: info.Mode().Perm()}
		switch {
		case info.Mode()&fs.ModeSymlink != 0:
			n.kind = kindSymlink
			tgt, err := os.Readlink(p)
			if err != nil {
				return err
			}
			n.target = tgt
		case d.IsDir():
			n.kind = kindDir
		default:
			n.kind = kindFile
			b, err := os.ReadFile(p)
			if err != nil {
				return err
			}
			n.content = b
		}
		out[filepath.ToSlash(rel)] = n
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// listAll returns every path under root, for the "nothing was written outside"
// assertions.
func listAll(t *testing.T, root string) []string {
	t.Helper()
	var out []string
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(root, p)
		if rel != "." {
			out = append(out, filepath.ToSlash(rel))
		}
		return nil
	})
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		t.Fatal(err)
	}
	sort.Strings(out)
	return out
}

// ---------------------------------------------------------------------------
// A fake control plane for the dedup exchange
// ---------------------------------------------------------------------------

// testJobID is the signed job id every test run carries. The exchange echoes
// it back and the agent checks it, so a fake that answered for a different job
// would be caught rather than ignored.
const testJobID = "44444444-4444-4444-4444-444444444444"

// fakeDedup is app/modules/backup/dedup.py in twenty lines: it holds a CHUNK
// INDEX (not the object store), answers `check` against it, records `stored`
// into it, and mints a grant scoped to the repository prefix.
//
// It is deliberately NOT backed by the object map. The real server answers
// from `backup_chunks`, so a chunk that exists as an object but was never
// reported is `new` — and a fake that peeked at the objects would hide an
// agent that stopped reporting `stored` at all.
type fakeDedup struct {
	mu sync.Mutex
	// index is what the control plane believes the repository holds.
	index map[string]bool
	// queries records every question asked, so a test can assert the BATCHING
	// rather than only the answers.
	queries []wire.ChunkQuery
	prefix  string
	// fail injects a transport failure at the n-th query (1-based); 0 never.
	failAt int
	// answerExtra makes the fake name a chunk the batch did not ask about.
	answerExtra string
	// wrongQueryID makes the fake answer a different question.
	wrongQueryID bool
	maxBytes     int64
}

func newFakeDedup(storageRef string) *fakeDedup {
	return &fakeDedup{index: map[string]bool{}, prefix: storageRef + "/", maxBytes: 64 << 20}
}

// hold pre-populates the index, standing in for chunks an EARLIER run of any
// device in this org already wrote.
func (d *fakeDedup) hold(ids ...string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	for _, id := range ids {
		d.index[id] = true
	}
}

func (d *fakeDedup) held() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]string, 0, len(d.index))
	for id := range d.index {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

func (d *fakeDedup) answer(_ context.Context, q wire.ChunkQuery) (wire.ChunkGrant, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.queries = append(d.queries, q)
	if d.failAt != 0 && len(d.queries) == d.failAt {
		return wire.ChunkGrant{}, fmt.Errorf("the control plane is unreachable")
	}
	if len(q.Check) > maxChunkBatch || len(q.Stored) > maxChunkBatch {
		return wire.ChunkGrant{}, fmt.Errorf("batch of %d/%d exceeds the bound %d",
			len(q.Check), len(q.Stored), maxChunkBatch)
	}
	for _, id := range q.Stored {
		d.index[id] = true
	}
	fresh := make([]string, 0, len(q.Check))
	for _, id := range q.Check {
		if !d.index[id] {
			fresh = append(fresh, id)
		}
	}
	if d.answerExtra != "" {
		fresh = append(fresh, d.answerExtra)
	}
	queryID := q.QueryID
	if d.wrongQueryID {
		queryID = "not-" + queryID
	}
	return wire.ChunkGrant{
		JobID:     q.JobID,
		QueryID:   queryID,
		New:       fresh,
		Recorded:  int64(len(q.Stored)),
		Upload:    wire.UploadGrant{URL: "https://s3.test/bucket", Fields: map[string]string{"policy": "x"}, KeyPrefix: d.prefix, MaxBytes: d.maxBytes},
		ExpiresAt: "2026-08-09T13:00:00Z",
	}, nil
}

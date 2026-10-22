package backup

import (
	"encoding/json"
	"fmt"
	"sort"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// The manifest: a backup point's whole contents, as one encrypted object
// (spec §5).

// maxManifestBytes bounds what this agent will decrypt into memory for one
// restore. A manifest is metadata — roughly 150 bytes per file — so 256 MiB is
// well over a million files, and it is a bound rather than a target: without
// one, a repository object of any size at all becomes an allocation of that
// size on an endpoint.
const maxManifestBytes = 256 << 20

// newEntry builds a ManifestEntry with the members the server's model requires
// present.
//
// `chunk_ids` is explicitly an EMPTY SLICE and never nil, because
// `ManifestEntry.chunk_ids` is `list[str] = []` server-side and does not accept
// null — and a nil Go slice marshals to null. A directory entry built by
// struct literal would therefore fail server-side validation for every
// directory in the tree, and schema_test.go asserts against exactly that.
func newEntry(p, kind string, mode uint32, size int64, mtime float64) manifestEntry {
	return manifestEntry{
		Path:     p,
		Kind:     kind,
		Mode:     mode,
		Size:     size,
		Mtime:    mtime,
		ChunkIDs: []string{},
		Target:   nil,
	}
}

// mtimeOf renders a modification time as the manifest's float seconds.
//
// A float64 holds a Unix second count to microsecond resolution for any date
// this century, which is finer than any filesystem in the fleet records. The
// schema says `number`, so an integer here would silently discard sub-second
// resolution on every file — and schema_test.go pins the type for that reason.
func mtimeOf(t time.Time) float64 {
	return float64(t.Unix()) + float64(t.Nanosecond())/1e9
}

func mtimeTo(f float64) time.Time {
	sec := int64(f)
	nsec := int64((f - float64(sec)) * 1e9)
	return time.Unix(sec, nsec)
}

// sortEntries orders a stream so that a parent always precedes its children.
//
// Plain lexical order over slash-separated paths gives that for free (`/a`
// sorts before `/a/b`), and it makes a manifest a deterministic function of the
// tree: two runs over identical data produce identical bytes, which is what
// makes a diff between two backup points mean anything.
func sortEntries(entries []manifestEntry) {
	sort.Slice(entries, func(i, j int) bool { return entries[i].Path < entries[j].Path })
}

// sealManifest encrypts a manifest for storage.
//
// The layout is `nonce(12) || ciphertext || tag(16)` with no AAD, which is
// EXACTLY app/modules/backup/restore.py::seal_manifest — the server writes
// manifests in tests and reads every manifest this agent writes when an
// operator browses a backup point. A difference in nonce width, AAD or JSON
// encoding here is a manifest the console cannot open, discovered on the day
// somebody needs a restore.
func sealManifest(k *repoKey, m *backupManifest) ([]byte, error) {
	return sealJSON(k, m)
}

// sealJSON is sealManifest for either manifest version: the same codec.
func sealJSON(k *repoKey, m any) ([]byte, error) {
	raw, err := json.Marshal(m)
	if err != nil {
		return nil, fmt.Errorf("backup: manifest is not encodable: %w", err)
	}
	blob, err := k.seal(nil, raw)
	zeroBytes(raw) // the manifest is metadata about customer data; do not leave it lying in the heap
	return blob, err
}

// openManifest opens a schema_version 1 manifest. A v2 manifest is refused
// here; restore reads both through openManifestDoc.
func openManifest(k *repoKey, blob []byte) (*backupManifest, error) {
	m1, m2, err := openManifestDoc(k, blob)
	if err != nil {
		return nil, err
	}
	if m2 != nil {
		return nil, jobs.Refuse("backup.restore: the manifest is schema_version 2 and this caller "+
			"reads %d", manifestSchemaVersion)
	}
	return m1, nil
}

// openManifestDoc is sealJSON's inverse, plus the checks a document from
// storage has to survive before anything acts on it. Exactly one of the two
// documents is returned.
func openManifestDoc(k *repoKey, blob []byte) (*backupManifest, *manifestV2, error) {
	if len(blob) > maxManifestBytes {
		return nil, nil, jobs.Refuse("backup.restore: the manifest object is larger than %d bytes; "+
			"this agent will not decrypt an unbounded object into memory", maxManifestBytes)
	}
	raw, err := k.open(nil, blob)
	if err != nil {
		return nil, nil, err
	}
	defer zeroBytes(raw)

	// The parse errors below are NOT included: they quote the offending input,
	// which here is decrypted customer metadata.
	var probe struct {
		SchemaVersion int `json:"schema_version"`
	}
	if err := json.Unmarshal(raw, &probe); err != nil {
		return nil, nil, jobs.Refuse("backup.restore: the manifest object did not decrypt to a " +
			"BackupManifest")
	}
	switch probe.SchemaVersion {
	case manifestSchemaVersion:
		var m backupManifest
		if err := json.Unmarshal(raw, &m); err != nil {
			return nil, nil, jobs.Refuse("backup.restore: the manifest object did not decrypt to a " +
				"BackupManifest")
		}
		return &m, nil, nil
	case manifestSchemaV2:
		var m manifestV2
		if err := json.Unmarshal(raw, &m); err != nil {
			return nil, nil, jobs.Refuse("backup.restore: the manifest object did not decrypt to a " +
				"version 2 BackupManifest")
		}
		return nil, &m, nil
	default:
		// A version this agent does not implement is refused rather than
		// guessed at: guessing restores a subset of a document whose shape
		// changed, which looks successful and is missing files.
		return nil, nil, jobs.Refuse("backup.restore: manifest schema_version is %d and this agent "+
			"implements %d and %d", probe.SchemaVersion, manifestSchemaVersion, manifestSchemaV2)
	}
}

// filetreeEntries flattens the streams this agent understands.
//
// Streams of an unknown TYPE are skipped, not refused, and that asymmetry with
// schema_version above is deliberate. §5 makes `streams` the
// forward-compatibility hinge precisely so a later `block` (image-level) or
// `named` (app-aware) stream can be added; an old agent that refused a manifest
// containing one would be an old agent that cannot restore the FILES out of a
// newer backup point, which is the opposite of what a forward-compatible format
// is for. A version bump, by contrast, says the document's own shape changed.
func filetreeEntries(m *backupManifest) []manifestEntry {
	var out []manifestEntry
	for i := range m.Streams {
		if m.Streams[i].Type != streamTypeFiletree {
			continue
		}
		out = append(out, m.Streams[i].Entries...)
	}
	return out
}

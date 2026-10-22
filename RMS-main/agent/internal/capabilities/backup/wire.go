package backup

import (
	"encoding/json"
	"sort"
	"strings"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// THE EXPORTED SCHEMA IS THE CONTRACT.
//
// Every struct in this file implements one file under
// docs/kernel/wire-protocol/, and schema_test.go asserts the correspondence
// field by field rather than trusting this comment. The seven files are
// BackupRunParams, BackupRunResult, BackupRestoreParams, BackupRestoreResult,
// BackupManifest, ManifestEntry and FiletreeStream.
//
// The result models are `extra="forbid"` server-side: an added member does not
// get ignored, it raises in the result handler and fails the WHOLE job
// (app/jobs/engine.py::complete converts a raising handler to `failed`). The
// params models are ignored-on-unknown here (encoding/json's default), which is
// docs/kernel/wire-protocol/README.md's "loosen inbound before you tighten
// outbound": a strictly additive control-plane change must not take a deployed
// fleet offline.

// ---------------------------------------------------------------------------
// backup.run
// ---------------------------------------------------------------------------

// runParams implements BackupRunParams.schema.json.
//
// `sources` and `dedup_params` are declared `type: object` with
// `additionalProperties: true` and no properties at all, so the schema pins
// only that they are objects. Their INTERNAL shape is a second, undeclared
// contract, and it is read off the server's own tests rather than invented
// here: `backend/tests/test_backup_worker.py` and `test_backup_dispatch.py`
// build policies with `sources={"paths": [...]}`, and
// `app/modules/backup/repository.py::DEFAULT_DEDUP` is the dedup shape. Both
// are pinned in schema_test.go.
//
// Neither is a pointer even though both are optional. site.wake's params use
// pointers because `port` and `repeat` have NON-ZERO schema defaults, so
// "absent" and "zero" are different statements there. Here both defaults are
// `{}` — the empty object — which is exactly what the zero value of these
// structs decodes to, so a pointer would distinguish two things that mean the
// same thing.
type runParams struct {
	RepositoryID string      `json:"repository_id"`
	StorageRef   string      `json:"storage_ref"`
	Sources      sourcesSpec `json:"sources"`
	DedupParams  dedupSpec   `json:"dedup_params"`
	RepoKey      string      `json:"repo_key"`
	Snapshot     string      `json:"snapshot"`
}

// sourcesSpec is this agent's reading of the untyped `sources` object.
//
// `paths` is the key the control plane actually uses. An unknown key is
// ignored, but an ABSENT `paths` is refused rather than treated as "back up
// nothing": a policy that silently protects zero bytes and reports success is
// the single worst failure mode a backup product has, and it is indetectable
// from the console until the day of a restore.
type sourcesSpec struct {
	Paths   []string `json:"paths"`
	Exclude []string `json:"exclude"`
}

// dedupSpec is this agent's reading of the untyped `dedup_params` object,
// mirroring app/modules/backup/repository.py::DEFAULT_DEDUP.
//
// Zero means "the server did not say", and defaults() fills it in. The chunker
// parameters define the repository's CHUNK DOMAIN (spec §5: "changing them
// starts a new chunk domain"), so an agent that quietly substituted its own
// would fork the domain of a live repository and destroy its dedup ratio
// without any error anywhere.
type dedupSpec struct {
	Algo string `json:"algo"`
	Avg  int    `json:"avg"`
	Min  int    `json:"min"`
	Max  int    `json:"max"`
	Hash string `json:"hash"`
}

// runResult implements BackupRunResult.schema.json.
//
// No `omitempty` anywhere: the server model forbids extras but every member
// here is one it declares, and an omitted key would silently take a Pydantic
// default instead of the number this agent measured. `manifest_ref` and `error`
// are POINTERS because their schema type is `string | null` and the difference
// between "no manifest" and "a manifest named the empty string" is the
// difference between a run the server records as failed and one it records as
// success (`ingest_backup_result` checks `result.manifest_ref` for truthiness).
type runResult struct {
	State          string         `json:"state"`
	ManifestRef    *string        `json:"manifest_ref"`
	BytesProtected int64          `json:"bytes_protected"`
	BytesStored    int64          `json:"bytes_stored"`
	ChunkCount     int64          `json:"chunk_count"`
	DedupStats     map[string]any `json:"dedup_stats"`
	Error          *string        `json:"error"`
}

// ---------------------------------------------------------------------------
// backup.restore
// ---------------------------------------------------------------------------

// restoreParams implements BackupRestoreParams.schema.json. Every member is
// required, so none is a pointer.
//
// `target` is `type: object, additionalProperties: true` with no declared
// properties — again an undeclared second contract, and again read off the
// server rather than invented: `app/modules/backup/api.py::restore_point`
// sends the operator's `{"path": "..."}`, and `verify_point` sends `{}` with
// the docstring "the agent restores every entry in the manifest into a scratch
// target and checksums it, NEVER LANDING FILES AT ANY CALLER-CHOSEN
// DESTINATION". That sentence is enforced in restore.go, not merely quoted:
// a verify carrying a `path` is refused.
type restoreParams struct {
	RepositoryID string     `json:"repository_id"`
	StorageRef   string     `json:"storage_ref"`
	ManifestRef  string     `json:"manifest_ref"`
	Selection    []string   `json:"selection"`
	Target       targetSpec `json:"target"`
	Mode         string     `json:"mode"`
	RepoKey      string     `json:"repo_key"`
}

// targetSpec is this agent's reading of the untyped `target` object.
type targetSpec struct {
	Path string `json:"path"`
	// Conflict is §7.1's `target.conflict`: rename, skip or overwrite. Absent
	// from a format-1 server, which means overwrite (wire_v2.go).
	Conflict string `json:"conflict"`
}

// restoreResult implements BackupRestoreResult.schema.json.
//
// `mismatch` is `list[str] | null`, so a nil slice — which encoding/json emits
// as `null` — is legal and means "nothing was compared or nothing differed".
// It is the ONE place in this package where a file path deliberately leaves the
// endpoint, because §9's proven-restore is worthless without naming what failed.
// Paths still never reach a LOG line; see redact.go.
type restoreResult struct {
	State         string   `json:"state"`
	RestoredBytes int64    `json:"restored_bytes"`
	Verified      bool     `json:"verified"`
	Mismatch      []string `json:"mismatch"`
	Error         *string  `json:"error"`
}

// ---------------------------------------------------------------------------
// The pinned repository format (spec §5)
// ---------------------------------------------------------------------------

// manifestEntry implements ManifestEntry.schema.json.
//
// `chunk_ids` is `list[str] = []` server-side, which does NOT accept null, so
// it must never be a nil slice when marshalled — dirs and symlinks carry an
// empty list, not an absent one. `target` is `str | null` and is a pointer for
// that reason: a symlink to the empty string is not the same as not a symlink.
type manifestEntry struct {
	Path     string   `json:"path"`
	Kind     string   `json:"kind"`
	Mode     uint32   `json:"mode"`
	Size     int64    `json:"size"`
	Mtime    float64  `json:"mtime"`
	ChunkIDs []string `json:"chunk_ids"`
	Target   *string  `json:"target"`
}

// filetreeStream implements FiletreeStream.schema.json. `type` is a const in
// the schema, so it is a const here.
type filetreeStream struct {
	Type    string          `json:"type"`
	Entries []manifestEntry `json:"entries"`
}

// backupManifest implements BackupManifest.schema.json.
//
// The `streams` list is spec §5's forward-compatibility hinge: a later
// image-level `block` stream or app-aware `named` stream is an added element,
// not a format rewrite. v1 writes exactly one stream and reads any number,
// skipping types it does not know — which is what makes an OLD agent able to
// restore the filetree half of a NEWER manifest instead of refusing it whole.
type backupManifest struct {
	SchemaVersion int              `json:"schema_version"`
	Streams       []filetreeStream `json:"streams"`
}

const (
	manifestSchemaVersion = 1
	streamTypeFiletree    = "filetree"

	kindFile    = "file"
	kindDir     = "dir"
	kindSymlink = "symlink"

	modeRestore = "restore"
	modeVerify  = "verify"

	stateSuccess = "success"
	stateFailed  = "failed"

	snapshotVSS  = "vss"
	snapshotLVM  = "lvm"
	snapshotAPFS = "apfs"
)

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

// Chunker bounds. The DEFAULTS mirror repository.py::DEFAULT_DEDUP and are
// pinned to it by schema_test.go. The CEILINGS are this agent's own and exist
// for the idle budget: `max` is the size of the one buffer a backup run holds,
// so a repository row carrying `max: 4294967296` would be a 4 GiB allocation on
// an endpoint, requested by a value in a database column.
const (
	defaultDedupAlgo = "buzhash"
	defaultDedupHash = "blake3-256"
	defaultChunkAvg  = 1 << 20 // 1 MiB
	defaultChunkMin  = 1 << 19 // 512 KiB
	defaultChunkMax  = 1 << 22 // 4 MiB

	// hardChunkMax bounds the resident buffer regardless of what the repository
	// row says. 64 MiB is sixteen times the pinned default and still an order of
	// magnitude below "low-tens-of-MB at idle" mattering, because it is
	// allocated inside a job and released when the job ends.
	hardChunkMax = 64 << 20
	hardChunkMin = 1 << 9 // 512 B: below this the manifest outweighs the data
)

// defaults fills in anything the server left unset and validates the rest.
//
// Refuses rather than clamping. A repository whose dedup parameters this agent
// does not accept is not a transient condition, and silently substituting
// different ones would fork the repository's chunk domain — every chunk would
// miss dedup against every chunk written by an agent that DID accept them, and
// nothing would report an error.
func (d dedupSpec) defaults() (dedupSpec, error) {
	out := d
	if out.Algo == "" {
		out.Algo = defaultDedupAlgo
	}
	if out.Hash == "" {
		out.Hash = defaultDedupHash
	}
	if out.Avg == 0 {
		out.Avg = defaultChunkAvg
	}
	if out.Min == 0 {
		out.Min = defaultChunkMin
	}
	if out.Max == 0 {
		out.Max = defaultChunkMax
	}

	if out.Algo != defaultDedupAlgo {
		return out, jobs.Refuse("backup: repository chunker is %q and this agent implements only "+
			"%q. Chunking with a different algorithm would write chunks that miss dedup against "+
			"every chunk already in the repository, silently", clean(out.Algo), defaultDedupAlgo)
	}
	if out.Hash != defaultDedupHash {
		return out, jobs.Refuse("backup: repository chunk-id hash is %q and this agent implements "+
			"only %q. The chunk id IS the dedup key and the integrity check", clean(out.Hash),
			defaultDedupHash)
	}
	if out.Min < hardChunkMin || out.Min > out.Avg || out.Avg > out.Max || out.Max > hardChunkMax {
		return out, jobs.Refuse("backup: dedup_params min=%d avg=%d max=%d is not "+
			"%d <= min <= avg <= max <= %d; `max` is the size of the buffer this job would hold "+
			"resident", out.Min, out.Avg, out.Max, hardChunkMin, hardChunkMax)
	}
	if out.Avg&(out.Avg-1) != 0 {
		return out, jobs.Refuse("backup: dedup_params avg=%d is not a power of two. The "+
			"content-defined boundary is 'the low log2(avg) bits of the rolling hash are zero', "+
			"which is not expressible otherwise", out.Avg)
	}
	return out, nil
}

// roots returns the source roots, sorted and de-duplicated.
//
// Sorted because a manifest whose entry order depends on map iteration is a
// manifest that differs between two runs over identical data, which makes every
// diff and every reproducibility check useless. De-duplicated because two
// overlapping roots would otherwise produce two entries for the same path, and
// restore would write the file twice.
func (s sourcesSpec) roots() ([]string, error) {
	seen := make(map[string]struct{}, len(s.Paths))
	out := make([]string, 0, len(s.Paths))
	for _, p := range s.Paths {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		if strings.ContainsRune(p, 0) {
			return nil, jobs.Refuse("backup: a source path contains a NUL byte")
		}
		if _, dup := seen[p]; dup {
			continue
		}
		seen[p] = struct{}{}
		out = append(out, p)
	}
	if len(out) == 0 {
		return nil, jobs.Refuse("backup: the policy names no source paths (`sources.paths` is " +
			"absent or empty). Reporting success over zero bytes is the one failure a backup " +
			"product must never have: it is invisible from the console until the day of a restore")
	}
	sort.Strings(out)
	return out, nil
}

// parseRunParams unmarshals and validates, and does all of it before anything
// is opened. Nothing below this function reads a file, dials or allocates a
// buffer if any of it fails.
func parseRunParams(raw json.RawMessage) (runParams, error) {
	var p runParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return p, jobs.Refuse("backup.run: params are not a BackupRunParams")
	}
	switch p.Snapshot {
	case snapshotVSS, snapshotLVM, snapshotAPFS, snapshotAuto:
	default:
		return p, jobs.Refuse("backup.run: snapshot kind %q is not one of vss, lvm, apfs, auto",
			clean(p.Snapshot))
	}
	if p.StorageRef == "" {
		return p, jobs.Refuse("backup.run: storage_ref is empty; there is no repository to write to")
	}
	d, err := p.DedupParams.defaults()
	if err != nil {
		return p, err
	}
	p.DedupParams = d
	if _, err := p.Sources.roots(); err != nil {
		return p, err
	}
	return p, nil
}

func parseRestoreParams(raw json.RawMessage) (restoreParams, error) {
	var p restoreParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return p, jobs.Refuse("backup.restore: params are not a BackupRestoreParams")
	}
	switch p.Mode {
	case modeRestore:
		if p.Target.Path == "" {
			return p, jobs.Refuse("backup.restore: mode=restore with no target.path. Every byte " +
				"this capability writes goes under one root supplied by the control plane; there " +
				"is no restore-to-original path in the pinned contract and this agent will not " +
				"invent one")
		}
	case modeVerify:
		if p.Target.Path != "" {
			// api.py::verify_point sends `target={}` and says why: a verify
			// "never lands files at any caller-chosen destination". Honouring a
			// path here would turn the one restore mode that needs no operator
			// authorization over a destination into one that writes wherever it
			// is told.
			return p, jobs.Refuse("backup.restore: mode=verify carries a target.path. A verify " +
				"restores into a scratch directory this agent chooses and deletes; it never " +
				"lands files at a caller-chosen destination")
		}
	default:
		return p, jobs.Refuse("backup.restore: mode %q is not one of restore, verify", clean(p.Mode))
	}
	if p.ManifestRef == "" {
		return p, jobs.Refuse("backup.restore: manifest_ref is empty; there is nothing to restore")
	}
	if p.StorageRef == "" {
		return p, jobs.Refuse("backup.restore: storage_ref is empty; there is no repository to " +
			"read chunks from")
	}
	return p, nil
}

// clean bounds and strips a job-supplied string before it is formatted into a
// refusal reason.
//
// A refusal reason lands in the server's HASH-CHAINED AUDIT LOG. An audit
// record an attacker can write arbitrary bytes into is not an audit record, and
// the runner's own sanitiser is the second layer, not the only one. Kept short:
// the reason is clamped server-side anyway and the useful information is which
// value was rejected, not all of it.
func clean(s string) string {
	const max = 40
	var b strings.Builder
	for _, r := range s {
		if b.Len() >= max {
			b.WriteString("…")
			break
		}
		if r < 0x20 || r == 0x7f {
			b.WriteRune('?')
			continue
		}
		b.WriteRune(r)
	}
	return b.String()
}

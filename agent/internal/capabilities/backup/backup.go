// Package backup implements the `backup.run` and `backup.restore` capabilities
// (docs/modules/backup/spec.md, docs/kernel/wire-protocol/Backup*.schema.json).
//
// # What is here and what is not
//
// The control plane owns scheduling, retention, the repository catalogue, key
// management and every piece of metadata. It does NOT own a single byte of
// customer data: the bytes only ever move on the endpoint, and this package is
// the whole of that movement. It implements the repository format §5 pins —
// content-defined chunking, BLAKE3-256 chunk ids, AES-256-GCM chunk objects,
// an encrypted manifest of `streams` — and it implements a restore that is
// confined to one root.
//
// It does NOT implement volume snapshots. See "The snapshot seam" below; that
// is a stated gap, not an omission being glossed.
//
// # Dormancy: this is the capability the idle budget was written for
//
// Registration is a map insert. Nothing in this package opens a file, dials
// anything, starts a goroutine or takes a timer-wheel entry until a verified
// job arrives. `Process` is a zero-value Service that resolves its own wiring
// at JOB TIME; the chunker's 4 MiB buffer, the Buzhash tables and the object
// store all come into existence inside a job and are gone when it returns.
// budget_test.go measures that rather than describing it — including RSS
// returning to baseline after a large run, which is the assertion that catches
// the obvious wrong implementation of this module (read the file into memory,
// then chunk it).
//
// One goroutine. A backup run is IO-bound and a parallel uploader would buy
// throughput at the cost of N chunk buffers resident and a goroutine count that
// scales with the fleet's file counts. Sequential is the budget-respecting
// default and the module brief does not ask for more.
//
// # No listening port, and no allowance in listen_test.go (invariant #2)
//
// This package opens no socket at all. The object store is an INJECTED SEAM —
// the transport lives outside this package — so there is nothing here to
// listen, accept or bind, and `internal/transport/listen_test.go` needed no new
// entry. The absence of an entry is the assertion: if a future change to this
// package requires one, that change is turning a file-mover into a listener.
//
// # Processes this capability may spawn: none on Windows
//
// On Windows, not one: VSS is reached through IVssBackupComponents, never
// `powershell.exe` (a SYSTEM process spawning PowerShell trips ASR rules) and
// never `vssadmin` (`vssadmin delete shadows` is a ransomware signature). On
// Linux and macOS the snapshot providers run the fixed list in
// docs/modules/backup/agent-engine.md §2, only during a `backup.run`, never
// through a shell. Any addition extends that list.
//
// # The two other antivirus hazards, and what this package does about them
//
//   - CONTROLLED FOLDER ACCESS blocks writes to user directories by processes
//     that are not on Defender's allowlist. A restore hits this first and hits
//     it as `access is denied` on the very first file under a protected folder.
//     restore.go therefore does not abort the job on a per-entry write failure:
//     it records the path in `mismatch` and continues, so an operator sees
//     WHICH paths were blocked rather than a single opaque failure. Getting the
//     agent's binary allowlisted is a deployment step, and it is documented in
//     docs/modules/backup/agent-engine.md.
//   - MASS SEQUENTIAL READS OF USER DOCUMENTS FOLLOWED BY NETWORK EGRESS is,
//     behaviourally, indistinguishable from ransomware staging. Nothing in this
//     package can make that untrue — it is a truthful description of what a
//     backup does. What it can do is not add the other signals that turn a
//     behavioural score into a block: no process spawning, no shadow-copy
//     deletion, no writes back over the files it reads, and no renaming.
//
// # Every job is signed (invariant #1)
//
// Both capabilities take a jobs.VerifiedJob and nothing else. The runner has
// already checked the Ed25519 signature over the canonical bytes against the
// key pinned at enrollment, the target device, freshness, the nonce and the
// type, in that order. There is no second entry point into this package and it
// adds none.
package backup

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log/slog"
	"runtime"
	"runtime/debug"
	"sync/atomic"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// Job types. These match app/modules/backup/handler.py's registrations exactly;
// a mismatch is an `unknown_type` refusal for every backup job in the fleet.
const (
	JobTypeRun     = "backup.run"
	JobTypeRestore = "backup.restore"
)

// Repository identifies the repository a job addresses. It carries no key: the
// key is handled entirely inside this package and never reaches the transport.
type Repository struct {
	// ID is `repository_id` from the job params.
	ID string
	// StorageRef is the object-store prefix root, `{org_id}/backup/{id}`
	// (repository.py::create_repository). Every suffix below is relative to it.
	StorageRef string
	// JobID is the job whose signature authorizes this store's grants. A read
	// query names it, and the control plane resolves the repository, the
	// prefix and the tenancy from the job IT signed -- never from anything the
	// agent sends.
	JobID string
}

// ObjectStore is the repository transport, job-scoped.
//
// # Neither direction is a credential this agent holds
//
// This agent holds no object-store credential and must never be given one: a
// long-lived key with write access to every tenant object under the bucket,
// sitting on a machine whose threat model is "may be compromised", is
// invariant #3 inverted.
//
// WRITES travel under a short-lived, prefix-scoped S3 POST policy that the
// control plane mints and hands back on the dedup exchange (dedup.go,
// docs/kernel/wire-protocol/BackupChunkGrantMsg.schema.json). It authorises
// writes under `{storage_ref}/` and nowhere else, it is bounded by a
// `content-length-range`, and it dies with the job. That covers every write a
// `backup.run` performs: the new chunks and the manifest.
//
// READS travel under batched, per-object GET grants: SigV4 query signing
// cannot express a prefix-scoped GET, so the control plane signs one URL per
// object instead of one per prefix. What makes that tractable at a restore's
// scale -- a selection can run to six figures of chunks -- is that a read
// query is not one grant per `Get`: `restore.go` announces the order it is
// about to fetch in (via `BatchReader.Expect`, below), and `backupstore`
// (outside this package) turns that announcement into a WINDOW of grants
// fetched ahead of the read, not a round trip per chunk.
//
// The seam stays an interface for the reason it always was: the transport lives
// outside this package, so there is nothing here to dial, listen or bind.
type ObjectStore interface {
	// Put stores data at suffix UNDER A SERVER-ISSUED GRANT and returns the
	// SUFFIX it wrote.
	//
	// Not a full reference: the agent does not know the bucket and must not.
	// A POST grant carries a key prefix, never a bucket name, and an endpoint
	// that constructed a full reference would be naming a bucket the server
	// then trusts. The suffix travels back as `manifest_ref`, and
	// `dispatch.py::_resolve_manifest_ref` derives the real object reference
	// from ids the control plane is already authorized for.
	//
	// There is no credential-less overload and no unauthorized path: every
	// write this package performs carries a grant the control plane issued for
	// this job, this repository and this window.
	Put(ctx context.Context, grant wire.UploadGrant, suffix string, data []byte) (ref string, err error)
	// Get fetches by suffix, relative to the repository's storage_ref. Chunks
	// are addressed this way: a restore knows chunk ids, not references.
	Get(ctx context.Context, suffix string) ([]byte, error)
	// GetRef fetches by a full server-issued reference -- NOT the suffix Put
	// returns. A restore's `manifest_ref` is `BackupPoint.manifest_ref`,
	// which the control plane derived server-side (`dispatch.py::
	// _resolve_manifest_ref`) from the suffix a prior `Put` reported; by the
	// time it reaches this agent in restore params it is the full reference,
	// bucket and all. `backupstore`'s implementation derives the
	// repository-relative suffix back out of that reference before it queries
	// for a grant -- this package hands over the full reference and does not
	// need to know how.
	GetRef(ctx context.Context, ref string) ([]byte, error)
	// Close releases the transport. Deferred inside the job, so nothing is held
	// between jobs.
	Close() error
}

// BatchReader is an ObjectStore that can be told, in advance, the order in
// which objects are about to be fetched.
//
// It exists because a read grant is signed per object: without an order to
// look ahead through, a store must ask for one grant per Get, which is the
// six-figure round-trip problem the read exchange exists to remove. It is
// OPTIONAL -- a store that does not implement it still works, one object per
// grant, which is correct and slow.
type BatchReader interface {
	Expect(suffixes []string)
}

// Snapshot is a point-in-time view of one or more volumes.
//
// Translate maps an ORIGINAL path to its path inside the snapshot; the manifest
// always records the original, because a path containing a shadow-copy device
// name would be meaningless to an operator browsing a backup point and
// unrestorable afterwards.
type Snapshot interface {
	Translate(original string) string
	Release() error
}

// passthroughSnapshot reads the live filesystem. It is what a test injects and
// it is NOT a production default — see defaultOpenSnapshot.
type passthroughSnapshot struct{}

func (passthroughSnapshot) Translate(p string) string { return p }
func (passthroughSnapshot) Release() error            { return nil }

// Options wires the capability. Every field has a default; they exist so the
// format engine is testable without a shadow copy, an object store or a clock.
type Options struct {
	// OpenStore opens the repository transport for one job. Nil means there is
	// none — see ObjectStore's comment — and the job is refused.
	OpenStore func(ctx context.Context, repo Repository) (ObjectStore, error)
	// QueryChunks runs one dedup exchange over the agent's authenticated
	// connection (dedup.go). Nil means there is none, and `backup.run` is
	// refused: without it there is no dedup answer AND no write grant.
	QueryChunks QueryChunks
	// OpenSnapshot takes a volume snapshot covering roots. Nil defaults to
	// defaultOpenSnapshot, which refuses.
	OpenSnapshot func(ctx context.Context, kind string, roots []string) (Snapshot, error)
	// ScratchParent is where a `mode=verify` restore creates its scratch
	// directory. Empty means os.MkdirTemp's default.
	ScratchParent string
	// NewName mints the object name for a manifest. Defaults to 16 random bytes
	// as hex; injected so a test can assert on the suffix.
	NewName func() (string, error)
	Logger  *slog.Logger
}

// Service is the process-wide holder. The zero value is usable and is what
// `Process` is: it resolves its own defaults at JOB TIME, so registration stays
// a map insert and nothing here touches a disk while the agent is idle.
type Service struct {
	opts Options
	// runs and restores count COMPLETED jobs, so a dormancy assertion outside
	// this package can observe that this capability has not run. Two atomic
	// loads.
	runs     atomic.Int64
	restores atomic.Int64

	// Test seams, nil in production: the clock `not_after_start` is read
	// against, and the filesystem the walk reads.
	clock  func() time.Time
	treeFS func() treeFS
	// paths is the zero value (this host's rules) except in tests that prove
	// the Windows rules on another OS.
	paths pathRules
}

func (s *Service) now() time.Time {
	if s.clock != nil {
		return s.clock()
	}
	return time.Now()
}

func (s *Service) newTreeFS() treeFS {
	if s.treeFS != nil {
		return s.treeFS()
	}
	return newOSTreeFS()
}

// Process is the Service the default capability registry registers.
var Process = &Service{}

// New returns a Service with explicit wiring. Tests use it; production uses
// Process.
func New(o Options) *Service { return &Service{opts: o} }

// Bind wires the process-wide Service. It is what main.go calls, and it
// follows logs.Process.Bind and provision.Process.Bind exactly: it installs
// seams and does nothing else. It opens no file, dials nothing, takes no wheel
// entry and starts no goroutine -- the capability resolves everything else at
// JOB TIME, so an idle agent that will never run a backup pays nothing for
// this call.
//
// Without it both capabilities are registered but refuse, naming the missing
// transport, which is a better answer than `unknown_type` and is one the
// control plane can act on.
func (s *Service) Bind(o Options) { s.opts = o }

// Runs reports how many backup runs this process has completed.
func (s *Service) Runs() int64 { return s.runs.Load() }

// Restores reports how many restore/verify jobs this process has completed.
func (s *Service) Restores() int64 { return s.restores.Load() }

func (s *Service) logger() *slog.Logger {
	if s.opts.Logger != nil {
		return s.opts.Logger
	}
	return slog.Default()
}

func (s *Service) newName() (string, error) {
	if s.opts.NewName != nil {
		return s.opts.NewName()
	}
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("backup: no randomness for a manifest name: %w", err)
	}
	return hex.EncodeToString(b[:]), nil
}

// openStore opens the repository transport, or refuses.
func (s *Service) openStore(ctx context.Context, repo Repository) (ObjectStore, error) {
	if s.opts.OpenStore == nil {
		return nil, jobs.Refuse("backup: this agent has no repository transport. Writes are " +
			"authorized by the short-lived, prefix-scoped POST grant the control plane returns " +
			"on the dedup exchange, and this build has no transport wired to spend one. Standing " +
			"object-store credentials on an endpoint would be a long-lived key with write access " +
			"to every tenant's data on a machine assumed to be compromisable, so this agent does " +
			"not hold any")
	}
	return s.opts.OpenStore(ctx, repo)
}

// defaultOpenSnapshot is the production snapshot provider, and it refuses.
//
// # A stated gap, not a hidden one
//
// v1 needs a consistent view of a live filesystem: VSS on Windows, APFS local
// snapshots on macOS (spec §1, BK-01). Neither is implemented, and neither
// could be written here in a form anybody should trust — a Linux container has
// no VSS and no APFS, so an IVssBackupComponents binding written against
// documentation alone would be several hundred lines of COM vtable calls that
// COMPILE, ship to a fleet, and are first executed on a customer's machine. A
// vtable index off by one is a crash in a SYSTEM service.
//
// So the seam is here, narrow and one function wide, and the refusal is honest.
// `refused` and not `failed` is the specified answer for exactly this: "no
// volume shadow copy service" is a standing incapacity of the device, not a
// transient error, and the reason travels into the server's hash-chained audit
// log where an operator can see it.
//
// What a real implementation must do is written down in
// docs/modules/backup/agent-engine.md, including the COM call sequence and the
// reason it must not be `vssadmin`.
func defaultOpenSnapshot(_ context.Context, kind string, _ []string) (Snapshot, error) {
	switch kind {
	case snapshotVSS:
		return nil, jobs.Refuse("backup.run: no volume shadow copy service. This build has no " +
			"VSS provider, so it cannot take the consistent point-in-time view a file-level " +
			"backup of a live Windows system requires. Backing up the live filesystem instead " +
			"would produce a backup point that restores torn files and reports success")
	case snapshotAPFS:
		return nil, jobs.Refuse("backup.run: no APFS snapshot provider in this build. Backing " +
			"up the live filesystem instead would produce a backup point that restores torn " +
			"files and reports success")
	case snapshotLVM:
		return nil, jobs.Refuse("backup.run: no LVM snapshot provider in this build")
	case snapshotAuto:
		return nil, jobs.Refuse("backup.run: no snapshot provider in this build to select from")
	default:
		return nil, jobs.Refuse("backup.run: snapshot kind %q is not one of vss, lvm, apfs, auto",
			clean(kind))
	}
}

func (s *Service) openSnapshot(ctx context.Context, kind string, roots []string) (Snapshot, error) {
	if s.opts.OpenSnapshot != nil {
		return s.opts.OpenSnapshot(ctx, kind, roots)
	}
	return defaultOpenSnapshot(ctx, kind, roots)
}

// ---------------------------------------------------------------------------
// The two capabilities
// ---------------------------------------------------------------------------

// Run is the `backup.run` capability.
//
// # Why a failed backup returns a SUCCEEDED job carrying state="failed"
//
// This looks wrong and is not. `app/jobs/engine.py::complete` runs a job type's
// result handler ONLY for a succeeded job. `ingest_backup_result` is what moves
// the `backup_runs` row off `running` — to `success` with the byte counters, or
// to `failed` with an error — and it is that handler. An agent that returned a
// Go error for "the object store timed out" would produce a failed JOB and a
// `backup_runs` row stuck on `running` forever, with no error text anywhere an
// operator looks.
//
// So an operational failure is reported as a RESULT DOCUMENT: `state="failed"`
// with `error`, which is exactly what `BackupRunResult.error` and the `failed`
// arm of `ingest_backup_result` exist for. A REFUSAL is different — it is this
// agent declining to act at all, it never reaches a result handler by design,
// and it lands in the audit log through the runner.
func (s *Service) Run(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	o, err := s.execRun(ctx, job.ID(), job.Params())
	if err != nil {
		return nil, err
	}
	s.runs.Add(1)
	return o.wire(), nil
}

// Restore is the `backup.restore` capability, in both `restore` and `verify`
// modes. The same reasoning about failed-versus-refused applies.
func (s *Service) Restore(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	o, err := s.execRestore(ctx, job.ID(), job.Params())
	if err != nil {
		return nil, err
	}
	s.restores.Add(1)
	return o.wire(), nil
}

// failedRun builds the "the job ran and did not succeed" result document.
func failedRun(format string, args ...any) *runResult {
	msg := fmt.Sprintf(format, args...)
	return &runResult{
		State:       stateFailed,
		ManifestRef: nil,
		DedupStats:  map[string]any{},
		Error:       &msg,
	}
}

func failedRestore(format string, args ...any) *restoreResult {
	msg := fmt.Sprintf(format, args...)
	return &restoreResult{State: stateFailed, Verified: false, Error: &msg}
}

// releaseMemory hands the chunk buffer's pages back to the OS at the end of a
// job.
//
// A backup run allocates a `max`-sized buffer (4 MiB at the pinned defaults)
// plus a ciphertext buffer, and Go's allocator would ordinarily keep those
// pages for the next allocation — which, on an agent that takes one backup a
// night, is hours away. CLAUDE.md's requirement is that a heavy capability
// "allocate on demand and RELEASE AFTER"; without this the agent's resident set
// stays at its backup-time high-water mark until something else happens.
//
// Called once per job, on the way out. It is not free (it forces a GC), which
// is why it is not called per file.
func releaseMemory() {
	runtime.GC()
	debug.FreeOSMemory()
}
